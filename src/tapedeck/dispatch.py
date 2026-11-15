"""What a shim does when the code under test runs `claude ...`: match, then replay or record."""

from __future__ import annotations

import json
import os
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .cassette import Cassette, atomic_write_text, locked
from .matching import Matcher, Request
from .player import Fault, FaultError, play
from .recorder import record
from .settings import MISS_EXIT_CODE, Settings, SettingsError


@dataclass
class SessionState:
    """Shared across the shim processes of one session: which recordings were consumed."""

    calls: int = 0
    consumed: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "SessionState":
        if path is None or not path.exists():
            return cls()
        raw = json.loads(path.read_text() or "{}")
        return cls(calls=int(raw.get("calls", 0)), consumed=dict(raw.get("consumed", {})))

    def save(self, path: Path | None) -> None:
        if path is not None:
            atomic_write_text(path, json.dumps({"calls": self.calls, "consumed": self.consumed}))


def main(program: str, real_bin: str | None, argv: list[str]) -> int:
    out, err = sys.stdout.buffer, sys.stderr.buffer
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        return _fail(err, str(exc))
    if settings.mode == "passthrough":
        if not real_bin:
            return _fail(err, f"passthrough mode but no real {program!r} binary was found on PATH")
        os.execv(real_bin, [real_bin, *argv])
    stdin = read_stdin(settings.stdin_timeout)
    return run_call(program, real_bin, argv, stdin, os.getcwd(), settings, (out, err))


def run_call(
    program: str,
    real_bin: str | None,
    argv: list[str],
    stdin: bytes,
    cwd: str,
    settings: Settings,
    sinks: tuple[IO[bytes], IO[bytes]],
) -> int:
    matcher = Matcher(ignore_flags=settings.ignore_flags, match_on=settings.match_on)
    req = Request(program, argv, stdin.decode("utf-8", errors="replace"), cwd)
    state_lock = settings.state or settings.cassette

    with locked(state_lock):
        state = SessionState.load(settings.state)
        state.calls += 1
        call_number = state.calls
        cassette = Cassette.load(settings.cassette)
        chosen = None if settings.mode == "record" else _choose(matcher, req, cassette, state, settings.repeat)
        if chosen is not None:
            state.consumed[str(chosen)] = state.consumed.get(str(chosen), 0) + 1
        state.save(settings.state)

    if chosen is not None:
        rec = cassette.interactions[chosen]
        try:
            fault = Fault.parse(settings.fault or rec.fault or "", call_number)
        except FaultError as exc:
            return _fail(sinks[1], str(exc))
        _log(settings, {"call": call_number, "program": program, "argv": argv, "action": "replay",
                        "interaction": chosen, "fault": fault.kind or None})
        return play(rec, argv=argv, cwd=cwd, sinks=sinks, ignore_flags=settings.ignore_flags,
                    speed=settings.speed, fault=fault, effects=settings.effects)

    if settings.mode in ("record", "new"):
        if not real_bin:
            return _fail(sinks[1], f"cannot record: no real {program!r} binary found on PATH")
        rec = record(program, real_bin, argv, stdin, cwd, sinks=sinks, capture_effects=settings.effects)
        with locked(state_lock):
            cassette = Cassette.load(settings.cassette)
            cassette.interactions.append(rec)
            cassette.save(settings.cassette)
            index = len(cassette.interactions) - 1
            state = SessionState.load(settings.state)
            state.consumed[str(index)] = state.consumed.get(str(index), 0) + 1
            state.save(settings.state)
        _log(settings, {"call": call_number, "program": program, "argv": argv, "action": "record",
                        "interaction": index, "exit_code": rec.exit_code})
        return rec.exit_code

    _log(settings, {"call": call_number, "program": program, "argv": argv, "action": "miss"})
    explanation = matcher.explain_miss(req, cassette.interactions)
    return _fail(
        sinks[1],
        f"no recorded interaction matches this {program} call (cassette: {settings.cassette}).\n"
        f"{explanation}\n"
        "Re-record with TAPEDECK_MODE=new (keep existing) or TAPEDECK_MODE=record (start over).",
    )


def _choose(matcher: Matcher, req: Request, cassette: Cassette, state: SessionState, repeat: bool) -> int | None:
    hits = matcher.candidates(req, cassette.interactions)
    for idx in hits:  # recordings are consumed in order: retry #2 gets recording #2
        if state.consumed.get(str(idx), 0) == 0:
            return idx
    return hits[-1] if hits and repeat else None


def read_stdin(timeout: float) -> bytes:
    """Read piped stdin to EOF. A TTY, or a pipe that stays silent for `timeout` seconds
    (an inherited, never-closed stdin), counts as empty so the shim cannot hang on it."""
    stream = sys.stdin
    if stream is None or stream.closed:
        return b""
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return b""
    if os.isatty(fd):
        return b""
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
    except (OSError, ValueError):  # e.g. not selectable
        ready = [fd]
    if not ready:
        return b""
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _log(settings: Settings, entry: dict[str, Any]) -> None:
    if settings.log is None:
        return
    entry["ts"] = round(time.time(), 3)
    settings.log.parent.mkdir(parents=True, exist_ok=True)
    with locked(settings.log), open(settings.log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _fail(err: IO[bytes], message: str) -> int:
    err.write(f"tapedeck: {message}\n".encode())
    err.flush()
    return MISS_EXIT_CODE
