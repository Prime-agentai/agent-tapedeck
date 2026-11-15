"""Capture what a program did to its working directory, and replay it elsewhere."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .cassette import Effect, safe_relpath

DEFAULT_IGNORE = (".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
                  ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".tapedeck")
MAX_FILES = 20_000
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class FileState:
    digest: str
    executable: bool


class Snapshot:
    """Content hashes of every regular file under `root` (minus ignored dirs)."""

    def __init__(self, root: Path, files: dict[str, FileState], complete: bool) -> None:
        self.root, self.files, self.complete = root, files, complete

    @classmethod
    def take(cls, root: str | os.PathLike[str], ignore: tuple[str, ...] = DEFAULT_IGNORE) -> "Snapshot":
        base = Path(root).resolve()
        files: dict[str, FileState] = {}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not _ignored(d, ignore))
            for name in filenames:
                if _ignored(name, ignore):
                    continue
                full = Path(dirpath) / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if len(files) >= MAX_FILES:
                    _warn(f"more than {MAX_FILES} files under {base}; file effects not captured")
                    return cls(base, files, complete=False)
                rel = full.relative_to(base).as_posix()
                files[rel] = FileState(_digest(full, st.st_size), bool(st.st_mode & stat.S_IXUSR))
        return cls(base, files, complete=True)

    def effects_since(self, before: "Snapshot") -> list[Effect]:
        if not (self.complete and before.complete):
            return []
        effects: list[Effect] = []
        for rel in sorted(self.files):
            now = self.files[rel]
            if before.files.get(rel) == now:
                continue
            full = self.root / rel
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    _warn(f"{rel} is larger than {MAX_FILE_BYTES} bytes; not recorded")
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            effects.append(Effect(path=rel, op="write", data=data, executable=now.executable))
        effects += [Effect(path=rel, op="delete") for rel in sorted(set(before.files) - set(self.files))]
        return effects


def apply_effects(root: str | os.PathLike[str], effects: list[Effect]) -> None:
    base = Path(root).resolve()
    for eff in effects:
        target = base / safe_relpath(eff.path)
        # Resolve symlinks in the parent chain too, so a planted link cannot redirect a write.
        if not str(target.parent.resolve()).startswith(str(base)):
            raise PermissionError(f"effect path {eff.path!r} escapes {base}")
        if eff.op == "delete":
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.write_bytes(eff.data or b"")
        mode = target.stat().st_mode
        target.chmod(mode | 0o111 if eff.executable else mode & ~0o111)


def _ignored(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _digest(path: Path, size: int) -> str:
    h = hashlib.sha256(str(size).encode())
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def _warn(msg: str) -> None:
    print(f"tapedeck: warning: {msg}", file=sys.stderr)
