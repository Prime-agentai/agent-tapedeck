"""Keep credentials out of cassettes, which are meant to be committed.

Two layers: (1) literal values of secret-looking environment variables present at record
time, (2) well-known API token shapes. `scan_text()` runs the token detectors over an
existing cassette so CI or a pre-commit hook can refuse a leaky one.

Not covered: PEM key blocks and arbitrary high-entropy strings. Pass extra literals with
TAPEDECK_REDACT (comma-separated) or `Redactor(extra=[...])` for anything else.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    "openai-key": re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}"),
    "github-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google-api-key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "slack-token": re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    "bearer-token": re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/\-]{20,}=*"),
}
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
MIN_ENV_SECRET_LEN = 8
CWD_TOKEN = "@@TAPEDECK_CWD@@"


@dataclass(frozen=True)
class Finding:
    kind: str
    where: str
    excerpt: str


class Redactor:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        extra: list[str] | None = None,
        *,
        cwd: str | None = None,
        home: str | None = None,
        user: str | None = None,
    ) -> None:
        env = os.environ if env is None else env
        # The recording cwd becomes a token that replay swaps for the live cwd, so cassettes
        # are portable; the home directory becomes `~` so fixtures do not leak a username.
        roots = {cwd, os.path.realpath(cwd)} if cwd else set()
        self._cwd = sorted((r for r in roots if r and len(r) > 1), key=len, reverse=True)
        self._home = home if home is not None else str(Path.home())
        # pytest's basetemp (/tmp/pytest-of-<user>/) carries the username into paths and into
        # tools that derive names from the cwd (Claude Code's per-project memory dir).
        self._user = user if user is not None else local_username()
        literals = {
            value: name
            for name, value in env.items()
            if SECRET_ENV.search(name) and len(value) >= MIN_ENV_SECRET_LEN and not _boring(value)
        }
        extra = list(extra or []) + [v for v in env.get("TAPEDECK_REDACT", "").split(",") if v]
        for value in extra:
            literals[value] = "custom"
        # Longest first so a secret that contains another is replaced whole.
        self._literals = sorted(literals.items(), key=lambda kv: -len(kv[0]))

    def text(self, value: str) -> str:
        for root in self._cwd:
            value = value.replace(root, CWD_TOKEN)
        if len(self._home) > 1:
            value = value.replace(self._home, "~")
        if self._user:
            value = value.replace(f"pytest-of-{self._user}", "pytest-of-user")
        for secret, name in self._literals:
            value = value.replace(secret, f"<redacted:{name}>")
        for kind, pattern in PATTERNS.items():
            value = pattern.sub(f"<redacted:{kind}>", value)
        return value

    def data(self, value: bytes) -> bytes:
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
        redacted = self.text(decoded)
        return value if redacted == decoded else redacted.encode("utf-8")


def scan_text(value: str, where: str) -> list[Finding]:
    findings = []
    for kind, pattern in PATTERNS.items():
        for m in pattern.finditer(value):
            token = m.group(0)
            findings.append(Finding(kind, where, f"{token[:6]}...{token[-2:]}" if len(token) > 10 else "***"))
    return findings


def local_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry in some containers
        return ""


def _boring(value: str) -> bool:
    """Env values that merely look secret by name: booleans, paths, small numbers."""
    return value.lower() in {"true", "false", "none", "null"} or value.startswith(("/", "./", "~")) or value.isdigit()
