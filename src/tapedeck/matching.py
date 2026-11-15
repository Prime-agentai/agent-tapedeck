"""Decide which recorded interaction answers a live call.

Live calls differ from recordings in ways that do not matter: the working directory is a
fresh temp dir, session ids are new UUIDs, pytest's basetemp counter moved on. Requests are
normalized before comparison so those differences vanish, while the prompt and the flags
that change behaviour still have to match.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cassette import Interaction
from .redact import CWD_TOKEN

# Flags whose *value* is volatile per run. The flag must still be present; its value is ignored.
DEFAULT_IGNORE_FLAGS = ("--session-id", "--resume", "-r")

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PYTEST_TMP = re.compile(r"pytest-of-[^/\\\s\"']+[/\\]pytest-\d+")
_TMP_RANDOM = re.compile(r"\btmp[a-z0-9_]{8}\b")  # tempfile.mkdtemp()/mkstemp() names


@dataclass
class Request:
    program: str
    argv: list[str]
    stdin: str
    cwd: str


@dataclass
class Normalizer:
    cwd: str
    home: str = field(default_factory=lambda: str(Path.home()))
    tmp: str = field(default_factory=tempfile.gettempdir)

    def text(self, value: str) -> str:
        subs = [(CWD_TOKEN, "<cwd>"), (self.cwd, "<cwd>"), (os.path.realpath(self.cwd), "<cwd>")]
        subs += [(self.tmp, "<tmp>"), ("/tmp", "<tmp>"), (self.home, "<home>")]
        for old, new in sorted({s for s in subs if s[0] and len(s[0]) > 1}, key=lambda s: -len(s[0])):
            value = value.replace(old, new)
        value = _PYTEST_TMP.sub("<pytest>", value)
        value = _UUID.sub("<uuid>", value)
        return _TMP_RANDOM.sub("tmp<rand>", value)

    def argv(self, argv: list[str], ignore_flags: tuple[str, ...]) -> list[str]:
        out: list[str] = []
        skip = False
        for arg in argv:
            if skip:
                out.append("<ignored>")
                skip = False
                continue
            flag, eq, _ = arg.partition("=")
            if flag in ignore_flags:
                out.append(f"{flag}=<ignored>" if eq else flag)
                skip = not eq
                continue
            out.append(self.text(arg))
        return out


def flag_values(argv: list[str], flags: tuple[str, ...]) -> dict[str, str]:
    """Values of `flags` in argv, for both `--flag value` and `--flag=value` forms."""
    found: dict[str, str] = {}
    for i, arg in enumerate(argv):
        flag, eq, val = arg.partition("=")
        if flag in flags:
            if eq:
                found[flag] = val
            elif i + 1 < len(argv):
                found[flag] = argv[i + 1]
    return found


@dataclass
class Matcher:
    ignore_flags: tuple[str, ...] = DEFAULT_IGNORE_FLAGS
    match_on: tuple[str, ...] = ("argv", "stdin")

    def key(self, program: str, argv: list[str], stdin: str, cwd: str) -> tuple[str, list[str], str]:
        n = Normalizer(cwd=cwd)
        return program, n.argv(argv, self.ignore_flags), n.text(stdin)

    def matches(self, req: Request, rec: Interaction) -> bool:
        if rec.program != req.program:
            return False
        override: dict[str, Any] = rec.match or {}
        _, live_argv, live_stdin = self.key(req.program, req.argv, req.stdin, req.cwd)
        _, rec_argv, rec_stdin = self.key(rec.program, rec.argv, rec.stdin, _rec_cwd(rec, req))
        argv_rule = override.get("argv", "exact" if "argv" in self.match_on else "any")
        stdin_rule = override.get("stdin", "exact" if "stdin" in self.match_on else "any")
        return _check(argv_rule, live_argv, rec_argv, joined=" ".join(req.argv)) and _check(
            stdin_rule, live_stdin, rec_stdin, joined=req.stdin
        )

    def candidates(self, req: Request, interactions: list[Interaction]) -> list[int]:
        return [i for i, rec in enumerate(interactions) if self.matches(req, rec)]

    def explain_miss(self, req: Request, interactions: list[Interaction]) -> str:
        """Human-readable diff against the closest recorded call for the same program."""
        same = [(i, r) for i, r in enumerate(interactions) if r.program == req.program]
        if not same:
            programs = sorted({r.program for r in interactions}) or ["(none)"]
            return f"no recorded calls to {req.program!r}; cassette has: {', '.join(programs)}"
        live = _render(*self.key(req.program, req.argv, req.stdin, req.cwd))

        def score(item: tuple[int, Interaction]) -> float:
            rec = item[1]
            return difflib.SequenceMatcher(None, live, _render(*self.key(rec.program, rec.argv, rec.stdin, _rec_cwd(rec, req)))).ratio()

        idx, best = max(same, key=score)
        recorded = _render(*self.key(best.program, best.argv, best.stdin, _rec_cwd(best, req)))
        diff = difflib.unified_diff(
            recorded.splitlines(), live.splitlines(), f"recorded #{idx}", "live call", lineterm="", n=2
        )
        return "\n".join(diff) or "(normalized requests are identical; all matching recordings were consumed)"


def _rec_cwd(rec: Interaction, req: Request) -> str:
    """Recorder output uses CWD_TOKEN; a hand-written cassette may carry a literal path."""
    return rec.cwd if rec.cwd and rec.cwd != CWD_TOKEN else req.cwd


def _render(program: str, argv: list[str], stdin: str) -> str:
    return "\n".join([f"program: {program}", *(f"argv: {a}" for a in argv), "stdin:", stdin])


def _check(rule: Any, live: Any, recorded: Any, joined: str) -> bool:
    if rule == "any":
        return True
    if rule == "exact":
        return bool(live == recorded)
    if isinstance(rule, dict):
        if "contains" in rule:
            return str(rule["contains"]) in joined
        if "regex" in rule:
            return re.search(str(rule["regex"]), joined, re.DOTALL) is not None
    raise ValueError(f"unknown match rule {rule!r}; use 'exact', 'any', {{'contains': ...}} or {{'regex': ...}}")
