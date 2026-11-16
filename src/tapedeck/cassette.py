"""Cassette format: a versioned, diff-friendly JSON file of recorded CLI interactions.

One cassette can hold calls to several programs (claude, codex, gemini, ...). Each
interaction stores the request (argv, stdin, cwd), the response (timed stdout/stderr
events and the exit code) and the *effects*: files the program created, changed or
deleted under its working directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

try:  # POSIX advisory locks; tapedeck targets Linux and macOS.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

FORMAT_VERSION = 1
STDOUT, STDERR = 1, 2


class CassetteError(Exception):
    """Raised for unreadable, unsupported or unsafe cassettes."""


@dataclass
class Event:
    """One chunk of output (normally one line) written by the program."""

    t: float  # seconds since the program started
    fd: int  # 1 = stdout, 2 = stderr
    data: bytes

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"t": round(self.t, 4), "fd": self.fd}
        try:
            out["text"] = self.data.decode("utf-8")
        except UnicodeDecodeError:
            out["b64"] = base64.b64encode(self.data).decode("ascii")
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Event":
        data = raw["text"].encode("utf-8") if "text" in raw else base64.b64decode(raw["b64"])
        return cls(t=float(raw.get("t", 0.0)), fd=int(raw.get("fd", STDOUT)), data=data)


@dataclass
class Effect:
    """A file-system change the program made, relative to its working directory."""

    path: str
    op: Literal["write", "delete"]
    data: bytes | None = None
    executable: bool = False

    def __post_init__(self) -> None:
        self.path = safe_relpath(self.path)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "op": self.op}
        if self.op == "write":
            assert self.data is not None
            try:
                out["text"] = self.data.decode("utf-8")
            except UnicodeDecodeError:
                out["b64"] = base64.b64encode(self.data).decode("ascii")
            if self.executable:
                out["executable"] = True
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Effect":
        op = raw.get("op", "write")
        if op not in ("write", "delete"):
            raise CassetteError(f"unknown effect op {op!r}")
        data: bytes | None = None
        if op == "write":
            if "text" in raw:
                data = raw["text"].encode("utf-8")
            elif "b64" in raw:
                data = base64.b64decode(raw["b64"])
            else:
                raise CassetteError(f"write effect for {raw.get('path')!r} has no content")
        return cls(path=raw["path"], op=op, data=data, executable=bool(raw.get("executable")))


@dataclass
class Interaction:
    program: str
    argv: list[str]
    stdin: str = ""
    cwd: str = ""
    exit_code: int = 0
    events: list[Event] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    duration: float = 0.0
    match: dict[str, Any] | None = None  # overrides for hand-authored interactions
    fault: str | None = None  # a fault baked into the cassette, e.g. "hang"
    recorded_at: str = ""
    note: str = ""

    def stdout_bytes(self) -> bytes:
        return b"".join(e.data for e in self.events if e.fd == STDOUT)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "program": self.program,
            "request": {"argv": self.argv, "stdin": self.stdin, "cwd": self.cwd},
            "response": {
                "exit_code": self.exit_code,
                "duration": round(self.duration, 4),
                "events": [e.to_json() for e in self.events],
                "effects": [e.to_json() for e in self.effects],
            },
        }
        for key in ("match", "fault", "recorded_at", "note"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Interaction":
        req, resp = raw.get("request", {}), raw.get("response", {})
        return cls(
            program=raw["program"],
            argv=list(req.get("argv", [])),
            stdin=req.get("stdin", ""),
            cwd=req.get("cwd", ""),
            exit_code=int(resp.get("exit_code", 0)),
            duration=float(resp.get("duration", 0.0)),
            events=[Event.from_json(e) for e in resp.get("events", [])],
            effects=[Effect.from_json(e) for e in resp.get("effects", [])],
            match=raw.get("match"),
            fault=raw.get("fault"),
            recorded_at=raw.get("recorded_at", ""),
            note=raw.get("note", ""),
        )


@dataclass
class Cassette:
    interactions: list[Interaction] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Cassette":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.parse(p.read_text(encoding="utf-8"), str(p))

    @classmethod
    def parse(cls, text: str, source: str = "<cassette>") -> "Cassette":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CassetteError(f"{source}: not valid JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise CassetteError(f"{source}: not a cassette (top level is not an object)")
        version = raw.get("version")
        if version != FORMAT_VERSION:
            raise CassetteError(f"{source}: unsupported cassette version {version!r}")
        return cls([Interaction.from_json(i) for i in raw.get("interactions", [])])

    def to_json(self) -> dict[str, Any]:
        return {
            "version": FORMAT_VERSION,
            "generator": "tapedeck",
            "interactions": [i.to_json() for i in self.interactions],
        }

    def save(self, path: str | os.PathLike[str]) -> None:
        atomic_write_text(Path(path), json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n")


def safe_relpath(path: str) -> str:
    """Reject absolute paths and parent traversal so a tampered cassette cannot
    write outside the directory it is replayed into."""
    norm = path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or ".." in parts or not parts:
        raise CassetteError(f"unsafe effect path {path!r}: must be relative and inside the working directory")
    return "/".join(parts)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)  # cassettes are meant to be committed; mkstemp defaults to 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def locked(path: str | os.PathLike[str]) -> Iterator[None]:
    """Exclusive cross-process lock for read-modify-write of `path`. The lock file lives in
    the temp dir, keyed by the absolute path, so no litter lands next to committed cassettes."""
    key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"tapedeck-{key}.lock"
    with open(lock_path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)

