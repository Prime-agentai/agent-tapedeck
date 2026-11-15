"""A tiny agent pipeline: ask Claude Code to do a job headlessly, parse its stream-json,
and return the final result. This is the kind of code tapedeck exists to test."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROMPT = "Create a file named hello.txt containing exactly: hi from the agent. Then reply with exactly: done"


def run_agent(cwd: Path, prompt: str = PROMPT, timeout: float = 180) -> dict:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
         "--model", "haiku", "--allowedTools", "Write", "--strict-mcp-config"],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if proc.returncode != 0 or result is None or result.get("is_error"):
        raise RuntimeError(f"agent failed (exit {proc.returncode}): {proc.stderr[-500:]}")
    tools = [c["name"] for e in events if e.get("type") == "assistant"
             for c in e["message"]["content"] if c.get("type") == "tool_use"]
    return {"result": result["result"], "turns": result.get("num_turns"), "tools": tools}
