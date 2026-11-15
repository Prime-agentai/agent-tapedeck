"""Replay a recorded interaction: output, timing, exit code and file effects, plus faults.

Faults (TAPEDECK_FAULT, or a `fault` field on the interaction) test the unhappy paths that
are hard to provoke from a real agent:

    hang          emit the first event, then block until killed (watchdog/timeout tests)
    truncate:N    emit N events, skip file effects, exit 1 (agent crashed mid-stream)
    exit:N        replay everything, then exit N
    delay:S       wait S seconds before the first byte
    drop-effects  replay output but write no files (agent claimed work it never did)

Append `@K` to fire only on the K-th call of the session (1-based), e.g. `hang@1` to make
the first attempt hang and let the retry succeed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import IO

from .cassette import STDOUT, Event, Interaction
from .matching import flag_values
from .redact import CWD_TOKEN
from .snapshot import apply_effects


class FaultError(ValueError):
    pass


@dataclass
class Fault:
    kind: str = ""
    arg: float = 0.0

    @classmethod
    def parse(cls, spec: str, call_number: int) -> "Fault":
        spec = (spec or "").strip()
        if not spec:
            return cls()
        spec, at, when = spec.partition("@")
        if at:
            try:
                if int(when) != call_number:
                    return cls()
            except ValueError as exc:
                raise FaultError(f"bad fault call number in {spec}@{when}") from exc
        kind, _, arg = spec.partition(":")
        if kind in ("hang", "drop-effects"):
            return cls(kind)
        if kind in ("truncate", "exit", "delay"):
            try:
                return cls(kind, float(arg))
            except ValueError as exc:
                raise FaultError(f"fault {kind} needs a number, e.g. {kind}:1") from exc
        raise FaultError(f"unknown fault {kind!r}; expected hang, truncate:N, exit:N, delay:S, drop-effects")


def play(
    rec: Interaction,
    *,
    argv: list[str],
    cwd: str,
    sinks: tuple[IO[bytes], IO[bytes]],
    ignore_flags: tuple[str, ...],
    speed: float = 0.0,
    fault: Fault | None = None,
    effects: bool = True,
) -> int:
    fault = fault or Fault()
    subs = _substitutions(rec, argv, cwd, ignore_flags)
    events = [Event(e.t, e.fd, _sub(e.data, subs)) for e in rec.events]
    if fault.kind == "delay":
        time.sleep(fault.arg)
    if fault.kind == "hang":
        if events:
            _emit(events[0], sinks)
        while True:  # the caller's timeout is what ends this
            time.sleep(3600)
    if fault.kind == "truncate":
        for ev in events[: int(fault.arg)]:
            _emit(ev, sinks)
        return 1

    # Files land just before the final event: a consumer that acts on a closing `result`
    # event (as stream-json consumers do) sees the work already on disk, like a real run.
    last = len(events) - 1
    prev_t = 0.0
    for i, ev in enumerate(events):
        if i == last:
            _apply(rec, cwd, subs, effects and fault.kind != "drop-effects")
        if speed > 0 and ev.t > prev_t:
            time.sleep((ev.t - prev_t) / speed)
        prev_t = ev.t
        _emit(ev, sinks)
    if not events:
        _apply(rec, cwd, subs, effects and fault.kind != "drop-effects")
    return int(fault.arg) if fault.kind == "exit" else rec.exit_code


def _apply(rec: Interaction, cwd: str, subs: list[tuple[bytes, bytes]], enabled: bool) -> None:
    if not enabled or not rec.effects:
        return
    patched = []
    for eff in rec.effects:
        if eff.data is not None:
            eff = type(eff)(eff.path, eff.op, _sub(eff.data, subs), eff.executable)
        patched.append(eff)
    apply_effects(cwd, patched)


def _substitutions(rec: Interaction, argv: list[str], cwd: str, ignore_flags: tuple[str, ...]) -> list[tuple[bytes, bytes]]:
    """Recorded volatile values -> live values, so replayed output names *this* run's
    working directory and session id rather than the recording's."""
    subs: list[tuple[bytes, bytes]] = [(CWD_TOKEN.encode(), cwd.encode())]
    if rec.cwd and rec.cwd not in (cwd, CWD_TOKEN):  # hand-written cassettes may use a literal path
        subs.append((rec.cwd.encode(), cwd.encode()))
    live = flag_values(argv, ignore_flags)
    for flag, old in flag_values(rec.argv, ignore_flags).items():
        new = live.get(flag)
        if new and old and new != old and len(old) >= 6:
            subs.append((old.encode(), new.encode()))
    return sorted(subs, key=lambda s: -len(s[0]))


def _sub(data: bytes, subs: list[tuple[bytes, bytes]]) -> bytes:
    for old, new in subs:
        data = data.replace(old, new)
    return data


def _emit(ev: Event, sinks: tuple[IO[bytes], IO[bytes]]) -> None:
    sink = sinks[0] if ev.fd == STDOUT else sinks[1]
    sink.write(ev.data)
    sink.flush()
