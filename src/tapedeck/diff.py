"""Behavioural diff of two cassettes: did the agent *do* something different?

A byte diff of two recordings is useless: session ids, timings, token counts and thinking
text change on every run. `tapedeck diff` compares what matters instead, per interaction:

* request  - the prompt / argv / stdin sent to the agent
* exit     - the process exit code (and the `is_error` flag of the final result)
* tools    - the ordered tool calls the agent made (Claude Code stream-json transcripts)
* files    - files the agent created, changed or deleted
* result   - the final reply (compared structurally when both sides are JSON)
* stats    - turns, cost, duration (reported, never a failure)

Typical uses: re-record a cassette against a new model and see whether behaviour drifted;
review a re-recorded cassette in a pull request; gate CI on "no new tool calls / no new files".
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from .cassette import Cassette, Interaction
from .matching import DEFAULT_IGNORE_FLAGS, Normalizer
from .redact import CWD_TOKEN

KINDS = ("request", "exit", "tools", "files", "result", "added", "removed")
"""Change kinds that can fail a diff. `stats` is informational and never fails."""
DEFAULT_FAIL_ON = frozenset(KINDS)
ToolKey = Literal["name", "target"]

# Tool input fields that identify *what* a tool acted on, in priority order.
_TARGET_FIELDS = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query",
                  "subagent_type", "description", "prompt", "skill")
_MAX_TARGET = 80
_MAX_DETAIL_LINES = 40
_MAX_LINE = 160
NO_RESULT = "<no result event>"


@dataclass(frozen=True)
class ToolCall:
    name: str
    target: str = ""

    def label(self) -> str:
        return f"{self.name} {self.target}".rstrip()

    def key(self, by: ToolKey) -> str:
        return self.name if by == "name" else self.label()


@dataclass(frozen=True)
class FileState:
    op: str  # "write" | "delete"
    digest: str = ""
    size: int = 0
    executable: bool = False
    text: str | None = None  # decoded content, for line diffs

    def describe(self) -> str:
        if self.op == "delete":
            return "deleted"
        return f"{self.size}B sha256:{self.digest}" + (" +x" if self.executable else "")


@dataclass
class Behaviour:
    """What one recorded interaction did, with run-to-run noise removed."""

    program: str
    request: list[str]
    stdin: str
    exit_code: int
    is_error: bool | None
    tools: list[ToolCall]
    files: dict[str, FileState]
    result: str
    turns: int | None = None
    cost_usd: float | None = None
    duration: float = 0.0

    def request_key(self) -> tuple[str, tuple[str, ...], str]:
        return self.program, tuple(self.request), self.stdin

    def title(self) -> str:
        prompt = " ".join(self.request)
        if self.stdin:
            prompt += f" <stdin {len(self.stdin)}B>"
        return f"{self.program} {prompt}"[:100].rstrip()


def summarize(rec: Interaction, ignore_flags: tuple[str, ...] = DEFAULT_IGNORE_FLAGS) -> Behaviour:
    norm = Normalizer(cwd=rec.cwd or CWD_TOKEN)
    stdout = rec.stdout_bytes().decode("utf-8", errors="replace")
    messages = _json_lines(stdout)
    final = next((m for m in reversed(messages) if m.get("type") == "result"), None)
    if final is None and len(messages) == 1 and "result" in messages[0]:
        final = messages[0]  # --output-format json: a single result object
    if final is not None:
        result = str(final.get("result", ""))
    elif messages:
        result = NO_RESULT  # a JSON transcript that never finished: truncated, killed or crashed
    else:
        result = stdout  # plain-text CLI output (codex/gemini style, or --output-format text)
    files: dict[str, FileState] = {}
    for eff in rec.effects:
        if eff.op == "delete" or eff.data is None:
            files[eff.path] = FileState(op="delete")
            continue
        try:
            text: str | None = eff.data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        files[eff.path] = FileState(op="write", digest=hashlib.sha256(eff.data).hexdigest()[:12],
                                    size=len(eff.data), executable=eff.executable, text=text)
    return Behaviour(
        program=rec.program,
        request=norm.argv(rec.argv, ignore_flags),
        stdin=norm.text(rec.stdin),
        exit_code=rec.exit_code,
        is_error=bool(final["is_error"]) if final is not None and "is_error" in final else None,
        tools=[_tool_call(block, norm) for block in _tool_uses(messages)],
        files=files,
        result=norm.text(result),
        turns=_as_int(final.get("num_turns")) if final is not None else None,
        cost_usd=_as_float(final.get("total_cost_usd")) if final is not None else None,
        duration=rec.duration,
    )


@dataclass
class Change:
    kind: str
    summary: str
    detail: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "summary": self.summary, "detail": self.detail}


@dataclass
class PairDiff:
    index_a: int | None
    index_b: int | None
    title: str
    changes: list[Change] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.index_a is None:
            return "added"
        if self.index_b is None:
            return "removed"
        return "changed" if any(c.kind != "stats" for c in self.changes) else "same"

    def to_json(self) -> dict[str, Any]:
        return {"a": self.index_a, "b": self.index_b, "status": self.status, "title": self.title,
                "changes": [c.to_json() for c in self.changes]}


@dataclass
class CassetteDiff:
    pairs: list[PairDiff]

    def kinds(self) -> set[str]:
        """Change kinds present, including `added` / `removed` interactions."""
        out: set[str] = set()
        for pair in self.pairs:
            if pair.status in ("added", "removed"):
                out.add(pair.status)
            out.update(c.kind for c in pair.changes)
        return out

    def failing(self, fail_on: Iterable[str] = DEFAULT_FAIL_ON) -> set[str]:
        return self.kinds() & set(fail_on) - {"stats"}

    def to_json(self) -> dict[str, Any]:
        counts = {s: sum(1 for p in self.pairs if p.status == s) for s in ("same", "changed", "added", "removed")}
        return {"summary": counts, "kinds": sorted(self.kinds()), "pairs": [p.to_json() for p in self.pairs]}

    def render(self, *, verbose: bool = False) -> str:
        lines: list[str] = []
        for pair in self.pairs:
            if pair.status == "same" and not verbose:
                continue
            a = "-" if pair.index_a is None else f"#{pair.index_a}"
            b = "-" if pair.index_b is None else f"#{pair.index_b}"
            lines.append(f"{pair.status.upper():<8} {a} -> {b}  {pair.title}")
            for ch in pair.changes:
                lines.append(f"  {ch.kind:<7} {ch.summary}")
                lines.extend(f"          {d}" for d in ch.detail)
        s = self.to_json()["summary"]
        lines.append(f"tapedeck diff: {s['same']} same, {s['changed']} changed, "
                     f"{s['added']} added, {s['removed']} removed")
        return "\n".join(lines)


def diff_cassettes(a: Cassette, b: Cassette, *, pair_by: Literal["request", "index"] = "request",
                   tool_key: ToolKey = "target", ignore: Iterable[str] = (),
                   ignore_flags: tuple[str, ...] = DEFAULT_IGNORE_FLAGS) -> CassetteDiff:
    """Pair interactions of `a` (before) with `b` (after) and compare their behaviour.

    `pair_by="request"` aligns calls by normalized request (so an inserted or dropped call
    shows up as added/removed instead of shifting every later pair); `"index"` pairs by position.
    """
    left = [summarize(r, ignore_flags) for r in a.interactions]
    right = [summarize(r, ignore_flags) for r in b.interactions]
    skip = set(ignore)
    pairs: list[PairDiff] = []
    for i, j in _align(left, right, pair_by):
        if i is None:
            assert j is not None
            pairs.append(PairDiff(None, j, right[j].title()))
        elif j is None:
            pairs.append(PairDiff(i, None, left[i].title()))
        else:
            changes = [c for c in compare(left[i], right[j], tool_key=tool_key) if c.kind not in skip]
            pairs.append(PairDiff(i, j, right[j].title(), changes))
    return CassetteDiff(pairs)


def compare(a: Behaviour, b: Behaviour, *, tool_key: ToolKey = "target") -> list[Change]:
    changes: list[Change] = []
    if a.request_key() != b.request_key():
        changes.append(_compare_request(a, b))
    if a.exit_code != b.exit_code or a.is_error != b.is_error:
        changes.append(Change("exit", f"exit {a.exit_code} -> {b.exit_code}"
                              + (f", is_error {a.is_error} -> {b.is_error}" if a.is_error != b.is_error else "")))
    tools = _compare_tools(a.tools, b.tools, tool_key)
    if tools:
        changes.append(tools)
    changes.extend(_compare_files(a.files, b.files))
    result = _compare_result(a.result, b.result)
    if result:
        changes.append(result)
    stats = _compare_stats(a, b)
    if stats:
        changes.append(stats)
    return changes


# --- comparisons ---------------------------------------------------------------------------

def _compare_request(a: Behaviour, b: Behaviour) -> Change:
    detail: list[str] = []
    parts: list[str] = []
    if a.program != b.program:
        detail.append(f"~ program: {a.program} -> {b.program}")
    if a.request != b.request:
        parts.append("argv")
        ops = difflib.SequenceMatcher(a=a.request, b=b.request, autojunk=False).get_opcodes()
        for op, i1, i2, j1, j2 in ops:
            if op == "equal":
                continue
            if op == "replace" and i2 - i1 == j2 - j1:
                detail += [f"~ argv[{j}]: {_inline(x, y)}" for j, x, y in zip(range(j1, j2), a.request[i1:i2], b.request[j1:j2])]
            else:
                detail += [f"- argv[{i}]: {x}" for i, x in zip(range(i1, i2), a.request[i1:i2])]
                detail += [f"+ argv[{j}]: {y}" for j, y in zip(range(j1, j2), b.request[j1:j2])]
    if a.stdin != b.stdin:
        parts.append("stdin")
        detail += ["stdin:"] + [f"  {d}" for d in _unified(a.stdin.splitlines(), b.stdin.splitlines())]
    return Change("request", " and ".join(parts or ["program"]) + " changed", _clip(detail))


def _inline(old: str, new: str, context: int = 30) -> str:
    """Show only the changed span of a long argument: `...prefix[-old-]{+new+}suffix...`."""
    if len(old) <= 60 and len(new) <= 60:
        return f"{old} -> {new}"
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    while p > 0 and not old[p - 1].isspace():  # snap to word boundaries
        p -= 1
    q = 0
    while q < min(len(old), len(new)) - p and old[-1 - q] == new[-1 - q]:
        q += 1
    while q > 0 and not old[len(old) - q].isspace():
        q -= 1
    head = ("..." if p > context else "") + old[max(0, p - context):p]
    tail = new[len(new) - q:][:context] + ("..." if q > context else "")
    return f"{head}[-{old[p:len(old) - q]}-]{{+{new[p:len(new) - q]}+}}{tail}"


def _compare_tools(a: list[ToolCall], b: list[ToolCall], by: ToolKey) -> Change | None:
    ka, kb = [t.key(by) for t in a], [t.key(by) for t in b]
    if ka == kb:
        return None
    detail: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ka, b=kb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        detail += [f"- {t.label()}" for t in a[i1:i2]] + [f"+ {t.label()}" for t in b[j1:j2]]
    names_a, names_b = _counts(t.name for t in a), _counts(t.name for t in b)
    delta = [f"{n} {names_a.get(n, 0)}->{names_b.get(n, 0)}"
             for n in sorted(set(names_a) | set(names_b)) if names_a.get(n, 0) != names_b.get(n, 0)]
    summary = f"{len(a)} -> {len(b)} tool calls" + (f" ({', '.join(delta)})" if delta else ", same tools, different order or targets")
    return Change("tools", summary, _clip(detail))


def _compare_files(a: dict[str, FileState], b: dict[str, FileState]) -> list[Change]:
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(p for p in set(a) & set(b) if a[p] != b[p])
    if not (added or removed or changed):
        return []
    detail = [f"+ {p} ({b[p].describe()})" for p in added]
    detail += [f"- {p} (was {a[p].describe()})" for p in removed]
    for p in changed:
        old, new = a[p], b[p]
        if old.text is not None and new.text is not None and old.op == new.op == "write" and old.text != new.text:
            body = _unified(old.text.splitlines(), new.text.splitlines())
            plus = sum(1 for d in body if d.startswith("+"))
            minus = sum(1 for d in body if d.startswith("-"))
            detail.append(f"~ {p} (+{plus}/-{minus} lines)")
            detail += [f"  {d}" for d in body]
        else:
            detail.append(f"~ {p} ({old.describe()} -> {new.describe()})")
    parts = [f"{len(x)} {name}" for x, name in ((added, "new"), (removed, "no longer touched"), (changed, "changed")) if x]
    return [Change("files", ", ".join(parts), _clip(detail))]


def _compare_result(a: str, b: str) -> Change | None:
    if a == b:
        return None
    ja, jb = _maybe_json(a), _maybe_json(b)
    if ja is not _NOT_JSON and jb is not _NOT_JSON:
        if ja == jb:
            return None  # same JSON, different whitespace/key order
        paths = _json_diff(ja, jb, "$")
        return Change("result", f"JSON result differs at {len(paths)} path(s)", _clip(paths))
    detail = _unified(a.splitlines(), b.splitlines())
    return Change("result", "final reply text changed", _clip(detail))


def _compare_stats(a: Behaviour, b: Behaviour) -> Change | None:
    parts: list[str] = []
    if a.turns != b.turns and (a.turns is not None or b.turns is not None):
        parts.append(f"turns {a.turns} -> {b.turns}")
    if a.cost_usd != b.cost_usd and a.cost_usd is not None and b.cost_usd is not None:
        parts.append(f"cost ${a.cost_usd:.4f} -> ${b.cost_usd:.4f}")
    if a.duration and b.duration and abs(a.duration - b.duration) >= 0.5:
        parts.append(f"duration {a.duration:.1f}s -> {b.duration:.1f}s")
    return Change("stats", ", ".join(parts)) if parts else None


# --- helpers -------------------------------------------------------------------------------

def _align(left: list[Behaviour], right: list[Behaviour], pair_by: str) -> list[tuple[int | None, int | None]]:
    if pair_by == "index":
        n = max(len(left), len(right))
        return [(i if i < len(left) else None, i if i < len(right) else None) for i in range(n)]
    if pair_by != "request":
        raise ValueError(f"pair_by {pair_by!r}; expected 'request' or 'index'")
    ka = [json.dumps(x.request_key()) for x in left]
    kb = [json.dumps(x.request_key()) for x in right]
    out: list[tuple[int | None, int | None]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ka, b=kb, autojunk=False).get_opcodes():
        if op == "equal":
            out += list(zip(range(i1, i2), range(j1, j2)))
            continue
        # A replaced block: same program in the same slot is "the same call, prompt edited".
        olds, news = list(range(i1, i2)), list(range(j1, j2))
        while olds and news and left[olds[0]].program == right[news[0]].program:
            out.append((olds.pop(0), news.pop(0)))
        out += [(i, None) for i in olds] + [(None, j) for j in news]
    return out


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _tool_uses(messages: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """tool_use blocks from Claude Code stream-json `assistant` messages, in order."""
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        body = msg.get("message")
        content = body.get("content") if isinstance(body, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _tool_call(block: dict[str, Any], norm: Normalizer) -> ToolCall:
    name = str(block.get("name", "?"))
    raw = block.get("input")
    target = ""
    if isinstance(raw, dict):
        for key in _TARGET_FIELDS:
            if isinstance(raw.get(key), str) and raw[key].strip():
                target = norm.text(raw[key]).replace("<cwd>/", "").replace("<cwd>", ".")
                break
    target = " ".join(target.split())
    if len(target) > _MAX_TARGET:
        target = target[: _MAX_TARGET - 3] + "..."
    return ToolCall(name, target)


_NOT_JSON = object()


def _maybe_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return _NOT_JSON
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return _NOT_JSON


def _json_diff(a: Any, b: Any, path: str) -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}"
            if k not in a:
                out.append(f"+ {p} = {_short(b[k])}")
            elif k not in b:
                out.append(f"- {p} (was {_short(a[k])})")
            else:
                out += _json_diff(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _json_diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"~ {path}: {_short(a)} -> {_short(b)}"]


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _unified(a: list[str], b: list[str]) -> list[str]:
    return [line for line in difflib.unified_diff(a, b, lineterm="", n=1)
            if not line.startswith(("---", "+++", "@@"))]


def _clip(lines: list[str]) -> list[str]:
    lines = [line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 3] + "..." for line in lines]
    if len(lines) <= _MAX_DETAIL_LINES:
        return lines
    return lines[:_MAX_DETAIL_LINES] + [f"... {len(lines) - _MAX_DETAIL_LINES} more line(s)"]


def _counts(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


__all__ = ["Behaviour", "CassetteDiff", "Change", "FileState", "PairDiff", "ToolCall", "compare",
           "diff_cassettes", "summarize", "KINDS", "NO_RESULT"]
