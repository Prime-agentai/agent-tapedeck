from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

# A stand-in "agent": prints stream-json-ish lines, echoes stdin, writes and deletes files.
# Behaviour is steered by its arguments so tests can tell calls apart.
FAKE_AGENT = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, uuid
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    sid = args[args.index("--session-id") + 1] if "--session-id" in args else str(uuid.uuid4())
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd()}}), flush=True)
    print("progress on stderr", file=sys.stderr, flush=True)
    if "--write" in args:
        name = args[args.index("--write") + 1]
        os.makedirs(os.path.dirname(name) or ".", exist_ok=True)
        with open(name, "w") as fh:
            fh.write("written in " + os.getcwd() + "\\n")
    if "--delete" in args:
        os.remove(args[args.index("--delete") + 1])
    if "--leak" in args:
        dummy = "sk-" + "ant-" + "dummy" + "Z" * 24  # assembled at runtime: no token-shaped literal in the repo
        print("token " + dummy + " and " + os.environ.get("MY_API_KEY", ""), flush=True)
    counter = os.environ.get("FAKE_COUNTER")
    n = 0
    if counter:
        n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
        open(counter, "w").write(str(n))
    print(json.dumps({{"type": "result", "result": "echo:" + prompt.strip(), "session_id": sid, "n": n}}), flush=True)
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    """
)


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory on PATH holding a fake `claude` executable (the 'real' agent)."""
    bindir = tmp_path / "realbin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(FAKE_AGENT.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture(autouse=True)
def _src_on_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocesses (`python -m tapedeck`, pytester runs) import the package from src/ when
    it is not pip-installed, e.g. in a fresh clone."""
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", f"{SRC}{os.pathsep}{existing}" if existing else str(SRC))
