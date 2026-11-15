# Changelog

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
