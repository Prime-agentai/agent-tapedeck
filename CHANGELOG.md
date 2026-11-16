# Changelog

## 0.2.0 (2026-11-16)

- **`tapedeck diff`**: behavioural diff of two cassettes, or of one cassette against git `HEAD`. Compares requests, exit codes, ordered tool calls (Claude Code stream-json), file effects (with line diffs) and the final result (structurally when both sides are JSON); reports turns, cost and duration without failing. Request-aware alignment, `--fail-on`, `--ignore`, `--tool-key name`, `--ignore-flag`, `--json`. Python API: `tapedeck.diff_cassettes`.
- Example: `examples/cassettes/drift_haiku.json` and `drift_sonnet.json`, the same task recorded from real Claude Code on two models.
- `tapedeck scan` warns (exit code unchanged) when the local OS username appears as a word in a cassette, e.g. from `ls -l` output in a tool result.
- Fix: request normalization replaced path prefixes inside longer names (`/tmp` ate the start of `/tmpab12cd34`, `/home/user` matched `/home/username`), so random `tempfile` names in prompts were not canonicalized. Substitutions now stop at path-component boundaries.
- `Cassette.parse(text)` loads a cassette from a string.

## 0.1.0 (2026-11-15)

First release.

- Record and replay any agent CLI (`claude`, `codex`, `gemini` by default) through `PATH` shims.
- Cassettes capture timed stdout/stderr events, exit code and file effects (write/delete/exec bit).
- Portable tapes: the recording cwd is tokenized, `$HOME` becomes `~`, live cwd and `--session-id` are swapped in on replay.
- Normalized matching (cwd, home, temp dirs, pytest basetemp, UUIDs, volatile flags) with ordered consumption and a diff on miss (exit 97).
- Modes: `once`, `replay`, `record`, `new`, `passthrough`.
- Fault injection: `hang`, `truncate:N`, `exit:N`, `delay:S`, `drop-effects`, scoped with `@K`.
- Secret redaction at record time and `tapedeck scan` for committed cassettes; path-traversal and symlink-escape protection on replay.
- CLI: `exec`, `record`, `play`, `ls`, `show`, `scan`, `synth`, `shim`. pytest plugin with `tapedeck` fixture and marker. Zero runtime dependencies.
