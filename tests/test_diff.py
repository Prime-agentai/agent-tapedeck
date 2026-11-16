"""tapedeck diff: behavioural comparison of two cassettes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tapedeck.cassette import Cassette, CassetteError, Effect, Event, Interaction
from tapedeck.cli import main
from tapedeck.diff import NO_RESULT, ToolCall, compare, diff_cassettes, summarize

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "cassettes" / "hello_agent.json"


def agent_run(prompt: str = "do it", *, tools: list[tuple[str, dict[str, Any]]] = (),  # type: ignore[assignment]
              result: str = "done", files: dict[str, bytes | None] | None = None, exit_code: int = 0,
              turns: int = 1, cost: float = 0.01, duration: float = 1.0, finished: bool = True,
              program: str = "claude") -> Interaction:
    """A Claude Code stream-json run: init, one assistant message per tool call, final result."""
    msgs: list[dict[str, Any]] = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for i, (name, inp) in enumerate(tools):
        msgs.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"varies {i}"},
            {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}]}})
        msgs.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "ok"}]}})
    if finished:
        msgs.append({"type": "result", "subtype": "success", "is_error": exit_code != 0, "result": result,
                     "num_turns": turns, "total_cost_usd": cost, "session_id": "s"})
    effects = [Effect(p, "delete") if data is None else Effect(p, "write", data) for p, data in (files or {}).items()]
    return Interaction(program=program, argv=["-p", prompt, "--output-format", "stream-json", "--verbose"],
                       cwd="@@TAPEDECK_CWD@@", exit_code=exit_code, duration=duration, effects=effects,
                       events=[Event(0.01 * i, 1, (json.dumps(m) + "\n").encode()) for i, m in enumerate(msgs)])


def kinds(a: Interaction, b: Interaction, **kw: Any) -> dict[str, Any]:
    return {c.kind: c for c in compare(summarize(a), summarize(b), **kw)}


# --- summarize -----------------------------------------------------------------------------

def test_summarize_real_claude_recording() -> None:
    b = summarize(Cassette.load(EXAMPLE).interactions[0])
    assert [t.name for t in b.tools] == ["Bash", "Write"]
    assert b.tools[1] == ToolCall("Write", "hello.txt")  # recording cwd token stripped
    assert b.tools[0].target.startswith("printf")
    assert b.result == "done" and b.exit_code == 0 and b.is_error is False
    assert list(b.files) == ["hello.txt"] and b.files["hello.txt"].text == "hi from the agent"
    assert isinstance(b.turns, int) and b.turns >= 1


def test_summarize_plain_text_and_json_output_formats() -> None:
    text = Interaction(program="codex", argv=["exec", "hi"], events=[Event(0, 1, b"hello\nworld\n")])
    assert summarize(text).result == "hello\nworld\n" and summarize(text).tools == []
    single = Interaction(program="claude", argv=["-p", "x", "--output-format", "json"],
                         events=[Event(0, 1, b'{"type":"result","result":"ok","is_error":false,"num_turns":2}\n')])
    assert summarize(single).result == "ok" and summarize(single).turns == 2


def test_unfinished_transcript_has_no_result() -> None:
    b = summarize(agent_run(tools=[("Read", {"file_path": "a.py"})], finished=False))
    assert b.result == NO_RESULT and b.tools == [ToolCall("Read", "a.py")]


def test_volatile_request_parts_are_normalized() -> None:
    a = agent_run("fix /tmp/tmpab12cd34/x.py")
    b = agent_run("fix /tmp/tmpzz99yy88/x.py")
    a.argv += ["--session-id", "11111111-2222-3333-4444-555555555555"]
    b.argv += ["--session-id", "99999999-2222-3333-4444-555555555555"]
    assert "request" not in kinds(a, b)


# --- compare -------------------------------------------------------------------------------

def test_identical_runs_have_no_changes_even_with_different_thinking() -> None:
    a = agent_run(tools=[("Read", {"file_path": "@@TAPEDECK_CWD@@/a.py"})], files={"a.py": b"x"})
    assert compare(summarize(a), summarize(agent_run(tools=[("Read", {"file_path": "@@TAPEDECK_CWD@@/a.py"})],
                                                     files={"a.py": b"x"}))) == []


def test_tool_drift_is_reported_with_counts_and_sequence() -> None:
    a = agent_run(tools=[("Read", {"file_path": "a.py"}), ("Edit", {"file_path": "a.py"})])
    b = agent_run(tools=[("Read", {"file_path": "a.py"}), ("Bash", {"command": "rm -rf  build/\n"}),
                         ("Edit", {"file_path": "a.py"})])
    ch = kinds(a, b)["tools"]
    assert ch.summary == "2 -> 3 tool calls (Bash 0->1)"
    assert ch.detail == ["+ Bash rm -rf build/"]  # whitespace collapsed


def test_tool_key_name_ignores_target_changes() -> None:
    a = agent_run(tools=[("Bash", {"command": "pytest -q"})])
    b = agent_run(tools=[("Bash", {"command": "python -m pytest -q"})])
    assert "same tools, different order or targets" in kinds(a, b)["tools"].summary
    assert "tools" not in kinds(a, b, tool_key="name")


def test_long_tool_targets_are_truncated() -> None:
    b = summarize(agent_run(tools=[("Bash", {"command": "echo " + "x" * 200})]))
    assert len(b.tools[0].target) == 80 and b.tools[0].target.endswith("...")


def test_file_changes_new_removed_changed_and_line_diff() -> None:
    a = agent_run(files={"keep.txt": b"one\ntwo\n", "old.txt": b"o", "bin.dat": b"\xff\x00", "gone.txt": None})
    b = agent_run(files={"keep.txt": b"one\nTWO\nthree\n", "new.txt": b"n", "bin.dat": b"\xff\x01", "gone.txt": None})
    ch = kinds(a, b)["files"]
    assert ch.summary == "1 new, 1 no longer touched, 2 changed"
    assert any(d.startswith("+ new.txt (1B sha256:") for d in ch.detail)
    assert any(d.startswith("- old.txt (was 1B") for d in ch.detail)
    assert "~ keep.txt (+2/-1 lines)" in ch.detail and "  +three" in ch.detail
    assert any(d.startswith("~ bin.dat (2B sha256:") for d in ch.detail)


def test_write_vs_delete_and_exec_bit_are_changes() -> None:
    a, b = agent_run(files={"f": b"x"}), agent_run(files={"f": None})
    assert "~ f (1B sha256:" in kinds(a, b)["files"].detail[0] and "deleted" in kinds(a, b)["files"].detail[0]
    c = agent_run(files={"run.sh": b"x"})
    c.effects[0].executable = True
    assert "+x" in kinds(agent_run(files={"run.sh": b"x"}), c)["files"].detail[0]


def test_json_results_compare_structurally() -> None:
    a = agent_run(result='{"status": "ok", "files": ["a"], "score": 1}')
    same = agent_run(result='{"score":1,"files":["a"],"status":"ok"}')
    assert "result" not in kinds(a, same)
    b = agent_run(result='{"status": "fail", "files": ["a"], "extra": true}')
    ch = kinds(a, b)["result"]
    assert ch.summary == "JSON result differs at 3 path(s)"
    assert ch.detail == ['+ $.extra = true', '- $.score (was 1)', '~ $.status: "ok" -> "fail"']


def test_text_result_diff_and_exit_change() -> None:
    k = kinds(agent_run(result="done"), agent_run(result="failed: no tests", exit_code=1))
    assert k["result"].detail == ["-done", "+failed: no tests"]
    assert k["exit"].summary == "exit 0 -> 1, is_error False -> True"


def test_stats_alone_do_not_change_status_or_fail() -> None:
    d = diff_cassettes(Cassette([agent_run(turns=2, cost=0.01, duration=1.0)]),
                       Cassette([agent_run(turns=5, cost=0.05, duration=9.0)]))
    assert d.pairs[0].status == "same" and d.failing() == set()
    assert d.pairs[0].changes[0].summary == "turns 2 -> 5, cost $0.0100 -> $0.0500, duration 1.0s -> 9.0s"


# --- pairing -------------------------------------------------------------------------------

def test_inserted_call_is_added_not_a_shift() -> None:
    a = Cassette([agent_run("plan"), agent_run("build"), agent_run("review")])
    b = Cassette([agent_run("plan"), agent_run("lint"), agent_run("build"), agent_run("review")])
    d = diff_cassettes(a, b)
    assert [p.status for p in d.pairs] == ["same", "added", "same", "same"]
    assert d.failing() == {"added"}
    by_index = diff_cassettes(a, b, pair_by="index")
    assert [p.status for p in by_index.pairs] == ["same", "changed", "changed", "added"]


def test_edited_prompt_in_same_slot_pairs_as_request_change() -> None:
    d = diff_cassettes(Cassette([agent_run("plan v1")]), Cassette([agent_run("plan v2")]))
    assert len(d.pairs) == 1 and d.pairs[0].status == "changed"
    assert d.pairs[0].changes[0].kind == "request"
    assert d.failing({"tools"}) == set() and d.failing() == {"request"}


def test_different_program_in_slot_is_removed_plus_added() -> None:
    d = diff_cassettes(Cassette([agent_run("x")]), Cassette([agent_run("x", program="codex")]))
    assert sorted(p.status for p in d.pairs) == ["added", "removed"]


def test_ignore_drops_kinds_and_render_and_json() -> None:
    a = Cassette([agent_run(tools=[("Read", {"file_path": "a"})], result="r1")])
    b = Cassette([agent_run(tools=[("Edit", {"file_path": "a"})], result="r2")])
    d = diff_cassettes(a, b, ignore=["result"])
    assert d.kinds() == {"tools"}
    text = d.render()
    assert text.startswith("CHANGED  #0 -> #0  claude -p do it") and "- Read a" in text and "+ Edit a" in text
    assert text.endswith("0 same, 1 changed, 0 added, 0 removed")
    assert json.loads(json.dumps(d.to_json()))["summary"]["changed"] == 1
    with pytest.raises(ValueError):
        diff_cassettes(a, b, pair_by="nope")  # type: ignore[arg-type]


# --- cli -----------------------------------------------------------------------------------

def save(path: Path, *runs: Interaction) -> Path:
    Cassette(list(runs)).save(path)
    return path


def test_cli_exit_codes_and_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = save(tmp_path / "a.json", agent_run(tools=[("Read", {"file_path": "x"})]))
    b = save(tmp_path / "b.json", agent_run(tools=[("Read", {"file_path": "x"}), ("Bash", {"command": "ls"})]))
    assert main(["diff", str(a), str(a)]) == 0
    assert main(["diff", str(a), str(b)]) == 1
    assert "+ Bash ls" in capsys.readouterr().out
    assert main(["diff", str(a), str(b), "--fail-on", "none"]) == 0
    assert main(["diff", str(a), str(b), "--fail-on", "files,result"]) == 0
    assert main(["diff", str(a), str(b), "--ignore", "tools"]) == 0
    capsys.readouterr()
    assert main(["diff", str(a), str(b), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["failing"] == ["tools"] and out["pairs"][0]["changes"][0]["kind"] == "tools"
    assert main(["diff", str(a), str(b), "--fail-on", "bogus"]) == 2
    assert main(["diff", str(a), str(tmp_path / "missing.json")]) == 2
    (tmp_path / "bad.json").write_text("[1]")
    assert main(["diff", str(a), str(tmp_path / "bad.json")]) == 2


def test_cli_one_cassette_compares_against_git_head(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "tests" / "cassettes"
    sub.mkdir(parents=True)
    path = save(sub / "run.json", agent_run(result="v1"))
    subprocess.run([*git, "add", "."], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "c"], cwd=tmp_path, check=True)
    assert main(["diff", str(path)]) == 0
    save(path, agent_run(result="v2"))
    assert main(["diff", str(path)]) == 1
    assert "+v2" in capsys.readouterr().out
    assert main(["diff", str(path), "--git", "no-such-rev"]) == 2


def test_cassette_parse_rejects_non_object() -> None:
    with pytest.raises(CassetteError):
        Cassette.parse("[]", "x")


# --- real recordings -----------------------------------------------------------------------

def test_real_model_drift_example() -> None:
    """examples/cassettes/drift_*.json: the same task recorded from real Claude Code with
    --model haiku and --model sonnet. Assertions stick to facts visible in the files."""
    cassettes = EXAMPLE.parent
    a, b = Cassette.load(cassettes / "drift_haiku.json"), Cassette.load(cassettes / "drift_sonnet.json")
    d = diff_cassettes(a, b)
    assert [p.status for p in d.pairs] == ["changed"]
    changes = {c.kind: c for c in d.pairs[0].changes}
    assert changes["request"].detail == ["~ argv[6]: haiku -> sonnet"]
    assert "request" not in {c.kind for c in diff_cassettes(
        a, b, ignore_flags=("--session-id", "--model")).pairs[0].changes}
    before, after = summarize(a.interactions[0]), summarize(b.interactions[0])
    assert [t.name for t in before.tools] == ["Bash", "Edit", "Write", "Bash"]
    assert [t.name for t in after.tools] == ["Read", "Write", "Write", "Bash"]
    assert before.result.startswith("```json") and json.loads(after.result)["fixed"] is True
    assert {"tools", "files", "result"} <= set(changes)
    assert diff_cassettes(a, a).failing() == set()


def test_scan_warns_about_local_username_without_failing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tapedeck.cli import _warn_username
    p = tmp_path / "c.json"
    p.write_text('{"x": "drwxr-x 2 alice alice 4096 .", "y": "alicette malice"}')
    assert _warn_username([p], user="alice") == 2
    assert "TAPEDECK_REDACT=alice" in capsys.readouterr().err
    assert _warn_username([p], user="bob") == 0 and _warn_username([p], user="al") == 0
