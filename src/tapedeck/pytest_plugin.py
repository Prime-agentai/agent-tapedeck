"""pytest integration. Auto-registered through the `pytest11` entry point.

    def test_planner(tapedeck):            # cassette: tests/cassettes/<module>/<test>.json
        run_my_agent_pipeline()            # its `claude` subprocess calls hit the cassette
        assert tapedeck.calls()[0]["action"] == "replay"

    @pytest.mark.tapedeck("shared.json", fault="hang@1", bins=["claude", "codex"])
    def test_retry_after_hang(tapedeck): ...

Mode: `--tapedeck-mode` > TAPEDECK_MODE env > `once` (record if the cassette is missing,
replay otherwise). Use `--tapedeck-mode=replay` in CI so a missing cassette fails loudly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from .deck import DEFAULT_BINS, Deck, use_cassette
from .settings import MODES


_RESET_KEY = pytest.StashKey[set[Path]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("tapedeck")
    group.addoption("--tapedeck-mode", choices=MODES, default=None,
                    help="cassette mode for the tapedeck fixture (default: TAPEDECK_MODE or 'once')")
    parser.addini("tapedeck_cassette_dir", "directory for cassettes, relative to each test file", default="cassettes")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "tapedeck(cassette=None, *, mode=None, bins=None, fault='', speed=0, ignore_flags=None, match_on=None, "
        "effects=True): configure the tapedeck fixture for this test",
    )


@pytest.fixture
def tapedeck(request: pytest.FixtureRequest) -> Iterator[Deck]:
    marker = request.node.get_closest_marker("tapedeck")
    opts: dict[str, Any] = dict(marker.kwargs) if marker else {}
    name = (marker.args[0] if marker and marker.args else None) or opts.pop("cassette", None)
    opts.pop("cassette", None)
    base = Path(str(request.node.fspath)).parent / request.config.getini("tapedeck_cassette_dir")
    if name:
        cassette = Path(name) if Path(name).is_absolute() else base / name
    else:
        module = Path(str(request.node.fspath)).stem
        cassette = base / module / f"{_safe(request.node.name)}.json"
    mode = opts.pop("mode", None) or request.config.getoption("--tapedeck-mode") or os.environ.get("TAPEDECK_MODE", "once")
    # In a recording session a cassette shared by several tests is wiped once, by its first
    # user; later tests append instead of erasing what earlier tests recorded.
    seen: set[Path] = request.config.stash.setdefault(_RESET_KEY, set())
    key = cassette.resolve()
    reset = key not in seen
    seen.add(key)
    with use_cassette(
        cassette,
        mode=mode,
        bins=opts.pop("bins", None) or DEFAULT_BINS,
        fault=opts.pop("fault", ""),
        speed=opts.pop("speed", 0.0),
        ignore_flags=opts.pop("ignore_flags", None),
        match_on=opts.pop("match_on", None),
        effects=opts.pop("effects", True),
        reset=reset,
    ) as deck:
        if opts:
            raise pytest.UsageError(f"unknown tapedeck marker option(s): {', '.join(sorted(opts))}")
        yield deck


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")[:120]
