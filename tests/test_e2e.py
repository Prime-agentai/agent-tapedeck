"""End to end: real subprocesses through real shims. The 'real agent' is tests/conftest.py's
fake CLI; after recording it is deleted, so a passing replay proves nothing real ran."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tapedeck import Cassette, use_cassette
from tapedeck.redact import CWD_TOKEN

CLI = [sys.executable, "-m", "tapedeck"]


def run(args: list[str], stdin: str = "", timeout: float = 30, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, **kw)


def test_record_then_replay_without_the_real_binary(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    with use_cassette(cassette, mode="record", bins=["claude"]) as deck:
        live = run(["claude", "-p", "go", "--session-id", "11111111-1111-1111-1111-111111111111",
                    "--write", "src/out.txt", "--output-format", "stream-json"], stdin="the spec")
        assert deck.calls()[0]["action"] == "record"
    assert live.returncode == 0 and "echo:the spec" in live.stdout and "progress on stderr" in live.stderr
    rec = Cassette.load(cassette).interactions[0]
    assert rec.cwd == CWD_TOKEN and rec.effects[0].path == "src/out.txt"
    assert str(workdir) not in json.dumps(Cassette.load(cassette).to_json())  # portable: no absolute cwd

    (fake_bin / "claude").unlink()  # nothing real left to call
    replay_dir = tmp_path / "elsewhere"
    replay_dir.mkdir()
    with use_cassette(cassette, mode="replay", bins=["claude"]) as deck:
        got = run(["claude", "-p", "go", "--session-id", "22222222-2222-2222-2222-222222222222",
                   "--write", "src/out.txt", "--output-format", "stream-json"], stdin="the spec", cwd=replay_dir)
        assert deck.calls()[0]["action"] == "replay"
    assert got.returncode == 0 and got.stderr == "progress on stderr\n"
    lines = [json.loads(line) for line in got.stdout.splitlines()]
    assert lines[0]["session_id"] == "22222222-2222-2222-2222-222222222222"  # live session id swapped in
    assert lines[0]["cwd"] == str(replay_dir.resolve())  # live cwd swapped in
    assert (replay_dir / "src" / "out.txt").read_text() == f"written in {replay_dir.resolve()}\n"


def test_miss_exits_97_with_a_diff(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    with use_cassette(cassette, mode="record", bins=["claude"]):
        run(["claude", "-p", "plan the feature"])
    with use_cassette(cassette, mode="replay", bins=["claude"]) as deck:
        got = run(["claude", "-p", "plan the features"])
        assert deck.calls()[-1]["action"] == "miss"
    assert got.returncode == 97
    assert "-argv: plan the feature" in got.stderr and "+argv: plan the features" in got.stderr


def test_repeated_calls_replay_in_recorded_order(fake_bin: Path, workdir: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    cassette = tmp_path / "c.json"
    monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "counter"))
    with use_cassette(cassette, mode="record", bins=["claude"]):
        for _ in range(2):
            run(["claude", "-p", "retry me"])
    with use_cassette(cassette, mode="replay", bins=["claude"]):
        ns = [json.loads(run(["claude", "-p", "retry me"]).stdout.splitlines()[-1])["n"] for _ in range(3)]
    assert ns == [1, 2, 2]  # in order, then the last one repeats


def test_once_and_new_modes(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    with use_cassette(cassette, mode="once", bins=["claude"]) as deck:
        assert deck.mode == "record"
        run(["claude", "-p", "a"])
    with use_cassette(cassette, mode="once", bins=["claude"]) as deck:
        assert deck.mode == "replay"
        assert run(["claude", "-p", "b"]).returncode == 97
    with use_cassette(cassette, mode="new", bins=["claude"]) as deck:
        assert run(["claude", "-p", "a"]).returncode == 0
        assert run(["claude", "-p", "b"]).returncode == 0
        assert [c["action"] for c in deck.calls()] == ["replay", "record"]
    assert len(Cassette.load(cassette).interactions) == 2


def test_hang_fault_trips_the_callers_timeout(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    with use_cassette(cassette, mode="record", bins=["claude"]):
        run(["claude", "-p", "x"])
    with use_cassette(cassette, mode="replay", bins=["claude"], fault="hang@1"):
        with pytest.raises(subprocess.TimeoutExpired):
            run(["claude", "-p", "x"], timeout=1.5)
        assert run(["claude", "-p", "x"]).returncode == 0  # the retry (call 2) is healthy


def test_secrets_never_reach_the_cassette(fake_bin: Path, workdir: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "super-secret-value-42")
    cassette = tmp_path / "c.json"
    with use_cassette(cassette, mode="record", bins=["claude"]):
        live = run(["claude", "-p", "x", "--leak"])
    assert "super-secret-value-42" in live.stdout  # the live caller still sees the real output
    text = cassette.read_text()
    assert "super-secret-value-42" not in text and "dummyZZZZ" not in text
    assert "<redacted:MY_API_KEY>" in text and "<redacted:anthropic-key>" in text
    assert run(CLI + ["scan", str(cassette)]).returncode == 0


def test_passthrough_calls_the_real_binary(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    with use_cassette(tmp_path / "c.json", mode="passthrough", bins=["claude"]):
        got = run(["claude", "-p", "x"], stdin="hello")
    assert "echo:hello" in got.stdout and not (tmp_path / "c.json").exists()  # nothing recorded


def test_environment_is_restored(tmp_path: Path) -> None:
    before = dict(os.environ)
    with use_cassette(tmp_path / "c.json", mode="replay", bins=["claude"]) as deck:
        assert os.environ["PATH"].startswith(str(deck.shim_dir))
    assert dict(os.environ) == before and not deck.shim_dir.exists()


# --- CLI ----------------------------------------------------------------------------------

def test_cli_record_play_ls_show(fake_bin: Path, workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    rec = run(CLI + ["record", "-c", str(cassette), "--", "claude", "-p", "hi", "--write", "f.txt"], stdin="s")
    assert rec.returncode == 0 and "echo:s" in rec.stdout
    (workdir / "f.txt").unlink()
    played = run(CLI + ["play", "-c", str(cassette), "--", "claude", "-p", "hi", "--write", "f.txt"], stdin="s")
    assert played.returncode == 0 and played.stdout.replace(str(workdir), "") == rec.stdout.replace(str(workdir), "")
    assert (workdir / "f.txt").exists()
    listing = run(CLI + ["ls", str(cassette)]).stdout
    assert "claude" in listing and "-p hi --write f.txt" in listing
    shown = run(CLI + ["show", str(cassette), "0"])
    assert "echo:s" in shown.stdout and "[effect] write f.txt" in shown.stderr
    assert json.loads(run(CLI + ["show", str(cassette), "0", "--json"]).stdout)["program"] == "claude"
    assert run(CLI + ["show", str(cassette), "5"]).returncode == 2


def test_cli_exec_with_synth_cassette_and_scan(workdir: Path, tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    assert run(CLI + ["synth", "-c", str(cassette), "--result", '{"status":"ok"}', "--write", "plan.md=# plan",
                      "--when-stdin-contains", "Planner"]).returncode == 0
    assert run(CLI + ["synth", "-c", str(cassette), "--program", "codex", "--format", "text",
                      "--result", "codex says hi"]).returncode == 0
    script = ("import subprocess,json;"
              "o=subprocess.run(['claude','-p','x','--output-format','stream-json'],input='You are the Planner',"
              "capture_output=True,text=True).stdout.splitlines();"
              "print(json.loads(o[-1])['result']);"
              "print(subprocess.run(['codex','exec','y'],capture_output=True,text=True).stdout.strip())")
    got = run(CLI + ["exec", "-c", str(cassette), "--mode", "replay", "--", sys.executable, "-c", script])
    assert got.returncode == 0, got.stderr
    assert got.stdout.splitlines() == ['{"status":"ok"}', "codex says hi"]
    assert "2 call(s) intercepted, 0 unmatched" in got.stderr
    assert (workdir / "plan.md").read_text() == "# plan"
    assert run(CLI + ["scan", str(cassette)]).returncode == 0

    leaky = json.loads(cassette.read_text())
    leaky["interactions"][0]["response"]["events"][0]["text"] = "ghp_" + "a" * 36 + "\n"
    cassette.write_text(json.dumps(leaky))
    scanned = run(CLI + ["scan", str(cassette)])
    assert scanned.returncode == 1 and "github-token" in scanned.stdout and "a" * 36 not in scanned.stdout


def test_cli_shim_and_errors(tmp_path: Path) -> None:
    out = run(CLI + ["shim", "--dir", str(tmp_path / "shims"), "--bin", "claude"])
    assert out.returncode == 0 and f'export PATH="{tmp_path / "shims"}:$PATH"' in out.stdout
    assert os.access(tmp_path / "shims" / "claude", os.X_OK)
    unset = run([str(tmp_path / "shims" / "claude"), "-p", "x"], env={"PATH": os.environ["PATH"]})
    assert unset.returncode == 97 and "TAPEDECK_CASSETTE is not set" in unset.stderr
    assert run(CLI + ["ls", str(tmp_path / "nope.json")]).returncode == 2
    assert run(CLI + ["exec", "-c", "x.json", "--fault", "explode", "--", "true"]).returncode == 2
    assert run(CLI + ["synth", "-c", str(tmp_path / "s.json"), "--result", "x", "--write", "nocontent"]).returncode == 2
