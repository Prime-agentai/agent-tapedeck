"""Runtime settings. Shims are separate processes, so everything travels via environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .matching import DEFAULT_IGNORE_FLAGS

MODES = ("replay", "record", "new", "once", "passthrough")
MISS_EXIT_CODE = 97


class SettingsError(ValueError):
    pass


@dataclass
class Settings:
    cassette: Path
    mode: str = "replay"
    state: Path | None = None
    log: Path | None = None
    speed: float = 0.0
    fault: str = ""
    ignore_flags: tuple[str, ...] = DEFAULT_IGNORE_FLAGS
    match_on: tuple[str, ...] = ("argv", "stdin")
    effects: bool = True
    repeat: bool = True
    stdin_timeout: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        cassette = env.get("TAPEDECK_CASSETTE")
        if not cassette:
            raise SettingsError("TAPEDECK_CASSETTE is not set; run under `tapedeck exec` or the pytest fixture")
        mode = env.get("TAPEDECK_MODE", "replay")
        if mode not in MODES:
            raise SettingsError(f"TAPEDECK_MODE={mode!r}; expected one of {', '.join(MODES)}")
        if mode == "once":
            # `once` is resolved per session by the orchestrator. A lone shim cannot know whether
            # this is the first call of a recording session, so it degrades to `new`.
            mode = "new"
        try:
            speed = float(env.get("TAPEDECK_SPEED", "0") or 0)
            stdin_timeout = float(env.get("TAPEDECK_STDIN_TIMEOUT", "2") or 2)
        except ValueError as exc:
            raise SettingsError(f"bad numeric setting: {exc}") from exc
        return cls(
            cassette=Path(cassette),
            mode=mode,
            state=Path(env["TAPEDECK_STATE"]) if env.get("TAPEDECK_STATE") else None,
            log=Path(env["TAPEDECK_LOG"]) if env.get("TAPEDECK_LOG") else None,
            speed=speed,
            fault=env.get("TAPEDECK_FAULT", ""),
            ignore_flags=_csv(env.get("TAPEDECK_IGNORE_FLAGS")) or DEFAULT_IGNORE_FLAGS,
            match_on=_csv(env.get("TAPEDECK_MATCH")) or ("argv", "stdin"),
            effects=env.get("TAPEDECK_EFFECTS", "1") not in ("0", "false", "off"),
            repeat=env.get("TAPEDECK_REPEAT", "1") not in ("0", "false", "off"),
            stdin_timeout=stdin_timeout,
        )


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())
