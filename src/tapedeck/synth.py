"""Author interactions by hand, without a real agent or credentials.

`claude` output follows the headless `--output-format stream-json --verbose` shape observed
from Claude Code 2.1.x (system/init, assistant, result), trimmed to the fields consumers
commonly read. `json` emits the single result object; `text` emits the reply as plain stdout,
which also suits codex/gemini style CLIs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .cassette import STDOUT, Effect, Event, Interaction

FORMATS = ("stream-json", "json", "text")


def claude_events(result: str, *, is_error: bool = False, model: str = "claude-sonnet-5-5",
                  session_id: str | None = None, num_turns: int = 1) -> list[dict[str, Any]]:
    sid = session_id or str(uuid.uuid4())
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return [
        {"type": "system", "subtype": "init", "cwd": "@@TAPEDECK_CWD@@", "session_id": sid, "model": model,
         "tools": [], "mcp_servers": [], "permissionMode": "default", "apiKeySource": "none"},
        {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
         "message": {"id": f"msg_tapedeck_{sid[:8]}", "type": "message", "role": "assistant", "model": model,
                     "content": [{"type": "text", "text": result}], "stop_reason": None, "usage": usage}},
        {"type": "result", "subtype": "error_during_execution" if is_error else "success", "is_error": is_error,
         "result": result, "session_id": sid, "num_turns": num_turns, "duration_ms": 0, "duration_api_ms": 0,
         "total_cost_usd": 0, "usage": usage, "permission_denials": [], "stop_reason": "end_turn"},
    ]


def synth_interaction(
    program: str,
    result: str,
    *,
    fmt: str = "stream-json",
    exit_code: int = 0,
    writes: dict[str, str] | None = None,
    deletes: list[str] | None = None,
    match: dict[str, Any] | None = None,
    fault: str | None = None,
    note: str = "",
) -> Interaction:
    if fmt not in FORMATS:
        raise ValueError(f"format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if fmt == "text":
        lines = [result if result.endswith("\n") else result + "\n"]
    else:
        events = claude_events(result, is_error=exit_code != 0)
        lines = [json.dumps(e) + "\n" for e in events] if fmt == "stream-json" else [json.dumps(events[-1]) + "\n"]
    effects = [Effect(path=p, op="write", data=c.encode("utf-8")) for p, c in (writes or {}).items()]
    effects += [Effect(path=p, op="delete") for p in deletes or []]
    return Interaction(
        program=program,
        argv=[],
        exit_code=exit_code,
        events=[Event(t=0.01 * i, fd=STDOUT, data=line.encode("utf-8")) for i, line in enumerate(lines)],
        effects=effects,
        match=match or {"argv": "any", "stdin": "any"},
        fault=fault,
        note=note or "synthesized by tapedeck synth",
    )
