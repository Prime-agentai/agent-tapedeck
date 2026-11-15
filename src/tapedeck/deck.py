"""`use_cassette()`: put shims for agent CLIs on PATH for the duration of a block.

Everything that spawns `claude`/`codex`/`gemini` inside the block, in this process or any
child process, hits the cassette instead of the real tool (or records the real tool).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, MutableMapping, Sequence

from .cassette import Cassette, Interaction
from .settings import MODES

DEFAULT_BINS = ("claude", "codex", "gemini")

_SHIM_PY = """\
import sys
sys.path.insert(0, {pkg_parent!r})
from tapedeck.dispatch import main
sys.exit(main(sys.argv[1], sys.argv[2] or None, sys.argv[3:]))
"""


@dataclass
class Deck:
    cassette: Path
    mode: str
    shim_dir: Path
    env: dict[str, str]
    log: Path

    def calls(self) -> list[dict[str, Any]]:
        """Every intercepted call so far: program, argv, action (replay/record/miss)."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    @property
    def interactions(self) -> list[Interaction]:
        return Cassette.load(self.cassette).interactions


def resolve_mode(mode: str, cassette: Path) -> str:
    if mode not in MODES:
        raise ValueError(f"mode {mode!r}; expected one of {', '.join(MODES)}")
    if mode == "once":
        return "replay" if cassette.exists() and Cassette.load(cassette).interactions else "record"
    return mode


def write_shims(shim_dir: Path, bins: Sequence[str], search_path: str) -> dict[str, str | None]:
    """Create one executable per CLI name; returns the real binary each one wraps."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    runner = shim_dir / "_tapedeck_shim.py"
    runner.write_text(_SHIM_PY.format(pkg_parent=str(Path(__file__).resolve().parent.parent)))
    real: dict[str, str | None] = {}
    for name in bins:
        found = shutil.which(name, path=_without(search_path, shim_dir))
        real[name] = found
        script = shim_dir / name
        script.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(runner))} "
            f"{shlex.quote(name)} {shlex.quote(found or '')} \"$@\"\n"
        )
        script.chmod(0o755)
    return real


@contextmanager
def use_cassette(
    path: str | os.PathLike[str],
    *,
    mode: str = "once",
    bins: Sequence[str] = DEFAULT_BINS,
    fault: str = "",
    speed: float = 0.0,
    ignore_flags: Sequence[str] | None = None,
    match_on: Sequence[str] | None = None,
    effects: bool = True,
    reset: bool = True,
    environ: MutableMapping[str, str] | None = None,
) -> Iterator[Deck]:
    """`reset=False` keeps existing interactions in record mode (for several tests sharing
    one cassette in a single recording session)."""
    environ = os.environ if environ is None else environ
    cassette = Path(path).resolve()
    effective = resolve_mode(mode, cassette)
    if effective == "record" and reset:
        Cassette().save(cassette)  # a recording session starts from an empty tape
    work = Path(tempfile.mkdtemp(prefix="tapedeck-"))
    shim_dir = work / "bin"
    write_shims(shim_dir, bins, environ.get("PATH", os.defpath))
    log = work / "calls.jsonl"
    overrides = {
        "PATH": f"{shim_dir}{os.pathsep}{environ.get('PATH', os.defpath)}",
        "TAPEDECK_CASSETTE": str(cassette),
        "TAPEDECK_MODE": effective,
        "TAPEDECK_STATE": str(work / "state.json"),
        "TAPEDECK_LOG": str(log),
        "TAPEDECK_SPEED": str(speed),
        "TAPEDECK_FAULT": fault,
        "TAPEDECK_EFFECTS": "1" if effects else "0",
    }
    if ignore_flags is not None:
        overrides["TAPEDECK_IGNORE_FLAGS"] = ",".join(ignore_flags)
    if match_on is not None:
        overrides["TAPEDECK_MATCH"] = ",".join(match_on)
    saved = {k: environ.get(k) for k in overrides}
    environ.update(overrides)
    try:
        yield Deck(cassette=cassette, mode=effective, shim_dir=shim_dir, env=dict(environ), log=log)
    finally:
        for key, value in saved.items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value
        shutil.rmtree(work, ignore_errors=True)


def _without(search_path: str, shim_dir: Path) -> str:
    skip = {str(shim_dir), str(shim_dir.resolve())}
    return os.pathsep.join(p for p in search_path.split(os.pathsep) if p and p not in skip)
