from __future__ import annotations

import io
import json
import os
import stat
import time
from pathlib import Path

import pytest

from tapedeck.cassette import Cassette, CassetteError, Effect, Event, Interaction, safe_relpath
from tapedeck.matching import Matcher, Normalizer, Request, flag_values
from tapedeck.player import Fault, FaultError, play
from tapedeck.redact import CWD_TOKEN, Redactor, scan_text
from tapedeck.settings import Settings, SettingsError
from tapedeck.snapshot import Snapshot, apply_effects
from tapedeck.synth import claude_events, synth_interaction


# --- cassette ---------------------------------------------------------------------------

def test_cassette_roundtrip_text_and_binary(tmp_path: Path) -> None:
    rec = Interaction(program="claude", argv=["-p", "hi"], stdin="in", exit_code=3,
                      events=[Event(0.1, 1, b"line\n"), Event(0.2, 2, b"\xff\xfe raw")],
                      effects=[Effect("a/b.txt", "write", b"x", executable=True), Effect("gone.txt", "delete")],
                      fault="hang", note="n")
    path = tmp_path / "c.json"
    Cassette([rec]).save(path)
    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert raw["interactions"][0]["response"]["events"][1].get("b64")
    back = Cassette.load(path).interactions[0]
    assert back.events[1].data == b"\xff\xfe raw" and back.exit_code == 3
    assert back.effects[0].executable and back.effects[1].op == "delete" and back.fault == "hang"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_cassette_rejects_unknown_version_and_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text('{"version": 99}')
    with pytest.raises(CassetteError, match="version"):
        Cassette.load(p)
    p.write_text("{nope")
    with pytest.raises(CassetteError, match="JSON"):
        Cassette.load(p)
    assert Cassette.load(tmp_path / "missing.json").interactions == []


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "C:/x", "", "."])
def test_safe_relpath_rejects_escapes(bad: str) -> None:
    with pytest.raises(CassetteError):
        safe_relpath(bad)


def test_safe_relpath_normalizes() -> None:
    assert safe_relpath("./a//b/c.txt") == "a/b/c.txt"


# --- matching ---------------------------------------------------------------------------

def test_normalizer_scrubs_volatile_values(tmp_path: Path) -> None:
    n = Normalizer(cwd=str(tmp_path / "proj"), home="/home/someone", tmp="/var/tmp")
    text = (f"{tmp_path}/proj/a.py 123e4567-e89b-12d3-a456-426614174000 /tmp/pytest-of-bob/pytest-41/x "
            f"/home/someone/.cfg {CWD_TOKEN}/b tmpab12cd_9")
    out = n.text(text)
    assert "<cwd>/a.py" in out and "<uuid>" in out and "<pytest>" in out and "<home>/.cfg" in out
    assert "<cwd>/b" in out and "tmp<rand>" in out


def test_ignore_flags_both_forms() -> None:
    n = Normalizer(cwd="/w")
    assert n.argv(["--session-id", "abc", "-p", "x"], ("--session-id",)) == ["--session-id", "<ignored>", "-p", "x"]
    assert n.argv(["--session-id=abc"], ("--session-id",)) == ["--session-id=<ignored>"]
    assert flag_values(["--resume", "s1", "--x=1"], ("--resume", "--x")) == {"--resume": "s1", "--x": "1"}


def test_matcher_rules() -> None:
    m = Matcher()
    rec = Interaction(program="claude", argv=["-p", "plan it"], stdin="spec v1")
    assert m.matches(Request("claude", ["-p", "plan it"], "spec v1", "/w"), rec)
    assert not m.matches(Request("claude", ["-p", "plan it"], "spec v2", "/w"), rec)
    assert not m.matches(Request("codex", ["-p", "plan it"], "spec v1", "/w"), rec)
    rec.match = {"argv": "any", "stdin": {"contains": "Builder"}}
    assert m.matches(Request("claude", ["whatever"], "You are the Builder Agent", "/w"), rec)
    rec.match = {"argv": {"regex": r"--model \w+"}, "stdin": "any"}
    assert m.matches(Request("claude", ["--model", "haiku"], "", "/w"), rec)
    rec.match = {"argv": "bogus"}
    with pytest.raises(ValueError):
        m.matches(Request("claude", [], "", "/w"), rec)
    assert Matcher(match_on=("argv",)).matches(Request("claude", ["-p", "plan it"], "other", "/w"),
                                               Interaction(program="claude", argv=["-p", "plan it"], stdin="x"))


def test_recorded_cwd_token_matches_live_cwd(tmp_path: Path) -> None:
    rec = Interaction(program="claude", argv=["--add-dir", f"{CWD_TOKEN}/src"], cwd=CWD_TOKEN)
    assert Matcher().matches(Request("claude", ["--add-dir", f"{tmp_path}/src"], "", str(tmp_path)), rec)


def test_explain_miss_shows_closest_diff() -> None:
    recs = [Interaction(program="claude", argv=["-p", "write tests"]), Interaction(program="claude", argv=["-p", "deploy"])]
    msg = Matcher().explain_miss(Request("claude", ["-p", "write test"], "", "/w"), recs)
    assert "recorded #0" in msg and "-argv: write tests" in msg and "+argv: write test" in msg
    assert "cassette has: claude" in Matcher().explain_miss(Request("codex", [], "", "/w"), recs)


# --- redaction --------------------------------------------------------------------------
# Token-shaped strings are assembled at runtime so no credential-shaped literal lives in the
# repo (secret scanners and push protection would rightly flag one).
DUMMY = {
    "anthropic-key": "sk-" + "ant-" + "dummy" + "a" * 20,
    "github-token": "gh" + "p_" + "d" * 36,
    "aws-access-key": "AK" + "IA" + "X" * 16,
}


def test_redactor_env_literals_patterns_home_and_cwd() -> None:
    env = {"MY_API_KEY": "s3cr3t-value-123", "DEBUG_TOKEN": "true", "HOME_PATH": "/x", "PLAIN": "s3cr3t-value-123x"}
    r = Redactor(env=env, cwd="/home/u/proj", home="/home/u", user="alice", extra=["custom-literal"])
    out = r.text(f"k=s3cr3t-value-123 a={DUMMY['anthropic-key']} {DUMMY['github-token']} "
                 "at /home/u/proj/f and /home/u/.claude -tmp-pytest-of-alice-pytest-3- custom-literal true")
    assert "alice" not in out and "pytest-of-user" in out
    assert "<redacted:MY_API_KEY>" in out and "<redacted:anthropic-key>" in out and "<redacted:github-token>" in out
    assert f"{CWD_TOKEN}/f" in out and "~/.claude" in out and "<redacted:custom>" in out
    assert out.endswith("true")  # boring values are not treated as secrets
    assert r.data(b"\xff binary") == b"\xff binary"


def test_scan_text_finds_tokens() -> None:
    findings = scan_text(f"x {DUMMY['aws-access-key']} y Bearer {'b' * 30} z", "here")
    assert {f.kind for f in findings} == {"aws-access-key", "bearer-token"}
    assert all("X" * 16 not in f.excerpt for f in findings)


# --- snapshot / effects ------------------------------------------------------------------

def test_snapshot_effects_write_change_delete_exec(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("same")
    (tmp_path / "change.txt").write_text("v1")
    (tmp_path / "remove.txt").write_text("bye")
    (tmp_path / ".git").mkdir()
    before = Snapshot.take(tmp_path)
    (tmp_path / "change.txt").write_text("v2")
    (tmp_path / "remove.txt").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.sh").write_text("#!/bin/sh\n")
    (tmp_path / "sub" / "new.sh").chmod(0o755)
    (tmp_path / ".git" / "ignored").write_text("x")
    effects = Snapshot.take(tmp_path).effects_since(before)
    by_path = {e.path: e for e in effects}
    assert set(by_path) == {"change.txt", "remove.txt", "sub/new.sh"}
    assert by_path["remove.txt"].op == "delete" and by_path["sub/new.sh"].executable

    target = tmp_path / "replay"
    target.mkdir()
    (target / "remove.txt").write_text("bye")
    apply_effects(target, effects)
    assert (target / "change.txt").read_text() == "v2" and not (target / "remove.txt").exists()
    assert os.access(target / "sub" / "new.sh", os.X_OK)


def test_apply_effects_refuses_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PermissionError):
        apply_effects(root, [Effect("link/pwned.txt", "write", b"x")])
    assert not (outside / "pwned.txt").exists()


# --- player -----------------------------------------------------------------------------

def _sinks() -> tuple[io.BytesIO, io.BytesIO]:
    return io.BytesIO(), io.BytesIO()


def test_fault_parse() -> None:
    assert Fault.parse("", 1).kind == ""
    assert Fault.parse("hang@2", 1).kind == "" and Fault.parse("hang@2", 2).kind == "hang"
    assert Fault.parse("truncate:3", 1) == Fault("truncate", 3.0)
    for bad in ("explode", "exit:x", "hang@x"):
        with pytest.raises(FaultError):
            Fault.parse(bad, 1)


def _rec_with_effect() -> Interaction:
    return Interaction(program="claude", argv=["--session-id", "recorded-sid-1"], cwd=CWD_TOKEN, exit_code=0,
                       events=[Event(0.0, 1, b'{"sid":"recorded-sid-1","cwd":"' + CWD_TOKEN.encode() + b'"}\n'),
                               Event(0.05, 2, b"warn\n"), Event(0.1, 1, b'{"type":"result"}\n')],
                       effects=[Effect("out.txt", "write", CWD_TOKEN.encode() + b"/out.txt")])


def test_play_substitutes_cwd_and_session_and_applies_effects(tmp_path: Path) -> None:
    out, err = _sinks()
    rc = play(_rec_with_effect(), argv=["--session-id", "live-sid-22"], cwd=str(tmp_path), sinks=(out, err),
              ignore_flags=("--session-id",))
    assert rc == 0 and err.getvalue() == b"warn\n"
    assert b"live-sid-22" in out.getvalue() and str(tmp_path).encode() in out.getvalue()
    assert (tmp_path / "out.txt").read_text() == f"{tmp_path}/out.txt"


def test_play_faults(tmp_path: Path) -> None:
    out, err = _sinks()
    assert play(_rec_with_effect(), argv=[], cwd=str(tmp_path), sinks=(out, err), ignore_flags=(),
                fault=Fault("truncate", 1)) == 1
    assert out.getvalue().count(b"\n") == 1 and not (tmp_path / "out.txt").exists()
    out, err = _sinks()
    assert play(_rec_with_effect(), argv=[], cwd=str(tmp_path), sinks=(out, err), ignore_flags=(),
                fault=Fault("drop-effects")) == 0
    assert b"result" in out.getvalue() and not (tmp_path / "out.txt").exists()
    assert play(_rec_with_effect(), argv=[], cwd=str(tmp_path), sinks=_sinks(), ignore_flags=(),
                fault=Fault("exit", 42)) == 42


def test_play_speed_respects_timing(tmp_path: Path) -> None:
    start = time.monotonic()
    play(_rec_with_effect(), argv=[], cwd=str(tmp_path), sinks=_sinks(), ignore_flags=(), speed=1.0)
    assert time.monotonic() - start >= 0.09


# --- settings / synth ---------------------------------------------------------------------

def test_settings_from_env() -> None:
    with pytest.raises(SettingsError):
        Settings.from_env({})
    with pytest.raises(SettingsError):
        Settings.from_env({"TAPEDECK_CASSETTE": "c.json", "TAPEDECK_MODE": "bogus"})
    s = Settings.from_env({"TAPEDECK_CASSETTE": "c.json", "TAPEDECK_MODE": "once", "TAPEDECK_IGNORE_FLAGS": "--a, --b",
                           "TAPEDECK_EFFECTS": "0", "TAPEDECK_SPEED": "2"})
    assert s.mode == "new" and s.ignore_flags == ("--a", "--b") and not s.effects and s.speed == 2.0


def test_synth_claude_shapes() -> None:
    events = claude_events('{"status":"ok"}')
    assert [e["type"] for e in events] == ["system", "assistant", "result"]
    assert events[-1]["result"] == '{"status":"ok"}' and events[-1]["subtype"] == "success"
    rec = synth_interaction("claude", "boom", fmt="json", exit_code=1, writes={"a.txt": "x"})
    assert len(rec.events) == 1 and json.loads(rec.events[0].data)["is_error"] is True
    assert synth_interaction("codex", "plain", fmt="text").events[0].data == b"plain\n"
    with pytest.raises(ValueError):
        synth_interaction("claude", "x", fmt="xml")
