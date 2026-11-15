"""Recorded once against real Claude Code (haiku), replayed forever after: no network, no
tokens, ~0.1s. Re-record with `pytest examples --tapedeck-mode=record`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_pipeline import run_agent


@pytest.mark.tapedeck("hello_agent.json", bins=["claude"])
def test_agent_writes_file_and_reports_done(tapedeck, tmp_path: Path) -> None:
    out = run_agent(tmp_path)
    assert out["result"] == "done"
    assert "Write" in out["tools"]
    assert (tmp_path / "hello.txt").read_text().strip() == "hi from the agent"
    assert [c["action"] for c in tapedeck.calls()] in (["replay"], ["record"])


@pytest.mark.tapedeck("hello_agent.json", bins=["claude"], fault="hang")
def test_pipeline_surfaces_a_hung_agent(tapedeck, tmp_path: Path) -> None:
    if tapedeck.mode != "replay":
        pytest.skip("fault injection only applies to replays")
    with pytest.raises(subprocess.TimeoutExpired):
        run_agent(tmp_path, timeout=1.5)


@pytest.mark.tapedeck("hello_agent.json", bins=["claude"], fault="drop-effects")
def test_agent_that_claims_work_it_did_not_do(tapedeck, tmp_path: Path) -> None:
    if tapedeck.mode != "replay":
        pytest.skip("fault injection only applies to replays")
    out = run_agent(tmp_path)
    assert out["result"] == "done" and not (tmp_path / "hello.txt").exists()  # trust, but verify
