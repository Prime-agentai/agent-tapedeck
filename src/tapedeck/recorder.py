"""Run the real CLI, pass its output through live, and capture everything it did."""

from __future__ import annotations

import datetime as dt
import signal
import subprocess
import threading
import time
from typing import IO, Any

from .cassette import Event, Interaction, STDERR, STDOUT
from .redact import CWD_TOKEN, Redactor
from .snapshot import Snapshot


def record(
    program: str,
    real_bin: str,
    argv: list[str],
    stdin: bytes,
    cwd: str,
    *,
    sinks: tuple[IO[bytes], IO[bytes]],
    capture_effects: bool = True,
    redactor: Redactor | None = None,
) -> Interaction:
    redactor = redactor or Redactor(cwd=cwd)
    before = Snapshot.take(cwd) if capture_effects else None
    start = time.monotonic()
    proc = subprocess.Popen(
        [real_bin, *argv], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    events: list[Event] = []
    lock = threading.Lock()

    def pump(stream: IO[bytes], fd: int, sink: IO[bytes]) -> None:
        for chunk in iter(stream.readline, b""):
            with lock:
                events.append(Event(time.monotonic() - start, fd, chunk))
            sink.write(chunk)
            sink.flush()

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, STDOUT, sinks[0]), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, STDERR, sinks[1]), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    restore = _forward_signals(proc)
    try:
        for t in threads:
            t.start()
        exit_code = proc.wait()
        for t in threads:
            t.join()
    finally:
        restore()
    duration = time.monotonic() - start
    effects = Snapshot.take(cwd).effects_since(before) if before is not None else []

    for ev in events:
        ev.data = redactor.data(ev.data)
    for eff in effects:
        if eff.data is not None:
            eff.data = redactor.data(eff.data)
    return Interaction(
        program=program,
        argv=[redactor.text(a) for a in argv],
        stdin=redactor.text(stdin.decode("utf-8", errors="replace")),
        cwd=CWD_TOKEN,
        exit_code=exit_code,
        events=sorted(events, key=lambda e: e.t),
        effects=effects,
        duration=duration,
        recorded_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )


def _forward_signals(proc: subprocess.Popen[bytes]) -> Any:
    """Forward SIGINT/SIGTERM to the child so a caller's timeout kills the real agent too."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda s, _f: proc.send_signal(s))

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return restore
