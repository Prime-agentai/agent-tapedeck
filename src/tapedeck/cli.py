"""tapedeck command line.

    tapedeck exec -c tests/cassettes/run.json -- pytest -q      # run anything with shims on PATH
    tapedeck record -c demo.json -- claude -p "hi" --output-format stream-json --verbose
    tapedeck play -c demo.json -- claude -p "hi" --output-format stream-json --verbose
    tapedeck ls demo.json | tapedeck show demo.json 0 | tapedeck scan tests/cassettes/*.json
    tapedeck synth -c demo.json --result '{"status":"ok"}' --write out.txt=hello
    tapedeck diff old.json new.json        # did the agent behave differently? (exit 1 if so)
    tapedeck diff tests/cassettes/run.json # working tree vs. git HEAD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .cassette import STDERR, Cassette, CassetteError, locked
from .deck import DEFAULT_BINS, use_cassette, write_shims
from .diff import KINDS, diff_cassettes
from .dispatch import read_stdin, run_call
from .matching import DEFAULT_IGNORE_FLAGS
from .player import Fault, FaultError
from .redact import local_username, scan_text
from .settings import MISS_EXIT_CODE, MODES, Settings
from .synth import FORMATS, synth_interaction


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (CassetteError, FaultError, ValueError) as exc:
        print(f"tapedeck: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tapedeck", description="Record and replay coding-agent CLIs for deterministic tests.")
    p.add_argument("--version", action="version", version=f"tapedeck {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def runtime_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-c", "--cassette", required=True, type=Path)
        sp.add_argument("--speed", type=float, default=0.0, help="0 = instant (default), 1 = recorded pace")
        sp.add_argument("--fault", default="", help="hang | truncate:N | exit:N | delay:S | drop-effects, optional @K")
        sp.add_argument("--no-effects", action="store_true", help="do not record/replay file changes")
        sp.add_argument("--ignore-flag", action="append", default=None, help="flag whose value is volatile (repeatable)")

    ex = sub.add_parser("exec", help="run a command with agent CLIs shimmed onto PATH")
    runtime_opts(ex)
    ex.add_argument("--mode", choices=MODES, default="once")
    ex.add_argument("--bin", action="append", default=None, help=f"CLI names to shim (default: {', '.join(DEFAULT_BINS)})")
    ex.add_argument("cmd", nargs=argparse.REMAINDER)
    ex.set_defaults(func=cmd_exec)

    for name, mode, desc in (("record", "record", "run the real CLI once and append it to the cassette"),
                             ("play", "replay", "replay one call from the cassette")):
        sp = sub.add_parser(name, help=desc)
        runtime_opts(sp)
        sp.add_argument("cmd", nargs=argparse.REMAINDER)
        sp.set_defaults(func=cmd_single, mode=mode)

    ls = sub.add_parser("ls", help="list interactions in a cassette")
    ls.add_argument("cassette", type=Path)
    ls.set_defaults(func=cmd_ls)

    show = sub.add_parser("show", help="print one interaction's output (or --json for all of it)")
    show.add_argument("cassette", type=Path)
    show.add_argument("index", type=int)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    scan = sub.add_parser("scan", help="fail if any cassette contains a credential-shaped string")
    scan.add_argument("cassettes", nargs="+", type=Path)
    scan.set_defaults(func=cmd_scan)

    shim = sub.add_parser("shim", help="write shims to a directory and print the env to use them (for CI)")
    shim.add_argument("--dir", required=True, type=Path)
    shim.add_argument("--bin", action="append", default=None)
    shim.set_defaults(func=cmd_shim)

    df = sub.add_parser("diff", help="behavioural diff of two cassettes: requests, exit, tools, files, result",
                        description="Compare what the agent did in two recordings. Exit 0 if no failing change, "
                                    "1 if a change of a --fail-on kind was found, 2 on error.")
    df.add_argument("old", type=Path, help="cassette before (or the only cassette, compared to --git REV)")
    df.add_argument("new", type=Path, nargs="?", default=None, help="cassette after")
    df.add_argument("--git", default="HEAD", metavar="REV",
                    help="with one cassette: compare REV:path (default HEAD) to the working tree")
    df.add_argument("--pair", choices=("request", "index"), default="request",
                    help="align calls by normalized request (default) or by position")
    df.add_argument("--tool-key", choices=("target", "name"), default="target",
                    help="compare tool calls by name+target (default) or by name only")
    df.add_argument("--ignore", default="", metavar="KINDS", help=f"comma list of kinds to skip: {','.join(KINDS[:5])}")
    df.add_argument("--fail-on", default=",".join(KINDS), metavar="KINDS",
                    help="comma list of kinds that make the exit code 1, or 'none' (default: all)")
    df.add_argument("--ignore-flag", action="append", default=None,
                    help="also ignore this flag's value, e.g. --ignore-flag=--model to compare models (repeatable)")
    df.add_argument("--json", action="store_true", help="machine-readable output")
    df.add_argument("-v", "--verbose", action="store_true", help="also list unchanged interactions")
    df.set_defaults(func=cmd_diff)

    sy = sub.add_parser("synth", help="append a hand-authored interaction (no agent, no credentials)")
    sy.add_argument("-c", "--cassette", required=True, type=Path)
    sy.add_argument("--program", default="claude")
    sy.add_argument("--result", required=True, help="final reply text (often the agent's JSON report)")
    sy.add_argument("--format", choices=FORMATS, default="stream-json")
    sy.add_argument("--exit", type=int, default=0, dest="exit_code")
    sy.add_argument("--write", action="append", default=[], metavar="PATH=CONTENT", help="file the agent 'writes'")
    sy.add_argument("--delete", action="append", default=[], metavar="PATH")
    sy.add_argument("--when-argv-contains", default=None)
    sy.add_argument("--when-stdin-contains", default=None)
    sy.add_argument("--fault", default=None)
    sy.set_defaults(func=cmd_synth)
    return p


def _strip_dashdash(cmd: list[str]) -> list[str]:
    return cmd[1:] if cmd and cmd[0] == "--" else cmd


def cmd_exec(args: argparse.Namespace) -> int:
    cmd = _strip_dashdash(args.cmd)
    if not cmd:
        raise ValueError("exec needs a command after --")
    if args.fault:
        Fault.parse(args.fault, 1)  # validate early
    with use_cassette(args.cassette, mode=args.mode, bins=args.bin or DEFAULT_BINS, fault=args.fault,
                      speed=args.speed, ignore_flags=args.ignore_flag, effects=not args.no_effects) as deck:
        print(f"tapedeck: {deck.mode} {deck.cassette}", file=sys.stderr)
        rc = subprocess.call(cmd)
        calls = deck.calls()
        misses = sum(1 for c in calls if c["action"] == "miss")
        print(f"tapedeck: {len(calls)} call(s) intercepted, {misses} unmatched", file=sys.stderr)
    return rc


def cmd_single(args: argparse.Namespace) -> int:
    cmd = _strip_dashdash(args.cmd)
    if not cmd:
        raise ValueError(f"{args.command} needs the agent command after --, e.g. -- claude -p hi")
    program = Path(cmd[0]).name
    real = shutil.which(cmd[0])
    env = dict(os.environ, TAPEDECK_CASSETTE=str(args.cassette), TAPEDECK_MODE=args.mode,
               TAPEDECK_FAULT=args.fault, TAPEDECK_SPEED=str(args.speed),
               TAPEDECK_EFFECTS="0" if args.no_effects else "1")
    if args.ignore_flag:
        env["TAPEDECK_IGNORE_FLAGS"] = ",".join(args.ignore_flag)
    settings = Settings.from_env(env)
    stdin = read_stdin(settings.stdin_timeout)
    return run_call(program, real, cmd[1:], stdin, os.getcwd(), settings, (sys.stdout.buffer, sys.stderr.buffer))


def cmd_ls(args: argparse.Namespace) -> int:
    cassette = _load_existing(args.cassette)
    print(f"{'#':>3}  {'program':<8} {'exit':>4} {'events':>6} {'files':>5} {'secs':>6}  request")
    for i, rec in enumerate(cassette.interactions):
        prompt = " ".join(rec.argv) + (f" <stdin {len(rec.stdin)}B>" if rec.stdin else "")
        if rec.match:
            prompt = f"[match {json.dumps(rec.match)}] {prompt}".strip()
        extra = f" fault={rec.fault}" if rec.fault else ""
        print(f"{i:>3}  {rec.program:<8} {rec.exit_code:>4} {len(rec.events):>6} {len(rec.effects):>5} "
              f"{rec.duration:>6.1f}  {prompt[:90]}{extra}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cassette = _load_existing(args.cassette)
    if not 0 <= args.index < len(cassette.interactions):
        raise ValueError(f"index {args.index} out of range (cassette has {len(cassette.interactions)})")
    rec = cassette.interactions[args.index]
    if args.json:
        print(json.dumps(rec.to_json(), indent=2, ensure_ascii=False))
        return 0
    for ev in rec.events:
        (sys.stderr.buffer if ev.fd == STDERR else sys.stdout.buffer).write(ev.data)
    for eff in rec.effects:
        print(f"[effect] {eff.op} {eff.path}", file=sys.stderr)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    total = 0
    for path in args.cassettes:
        cassette = _load_existing(path)
        for i, rec in enumerate(cassette.interactions):
            # Scan the stored JSON: catches secrets in argv, stdin, output and file effects alike.
            for f in scan_text(json.dumps(rec.to_json(), ensure_ascii=False), f"{path}#{i}"):
                print(f"{f.where}: {f.kind} {f.excerpt}")
                total += 1
    print(f"tapedeck scan: {total} finding(s) in {len(args.cassettes)} cassette(s)", file=sys.stderr)
    _warn_username(args.cassettes)
    return 1 if total else 0


def _warn_username(paths: list[Path], user: str | None = None) -> int:
    """Privacy hint, never a failure: tool output such as `ls -l` or `whoami` can carry the
    recording user's name into a cassette, where path tokenization cannot see it."""
    user = local_username() if user is None else user
    if len(user) < 3:
        return 0
    word = re.compile(rf"(?<![\w.-]){re.escape(user)}(?![\w-])")
    hits = 0
    for path in paths:
        n = len(word.findall(path.read_text(encoding="utf-8", errors="replace")))
        if n:
            hits += n
            print(f"tapedeck scan: warning: {path} contains your OS username {user!r} {n} time(s) "
                  f"(e.g. in ls/whoami output); re-record with TAPEDECK_REDACT={user} to scrub it", file=sys.stderr)
    return hits


def _kinds(value: str, flag: str) -> set[str]:
    if value.strip().lower() == "none":
        return set()
    kinds = {k.strip() for k in value.split(",") if k.strip()}
    unknown = kinds - set(KINDS) - {"stats"}
    if unknown:
        raise ValueError(f"{flag}: unknown kind(s) {', '.join(sorted(unknown))}; expected {', '.join(KINDS)}")
    return kinds


def _git_cassette(rev: str, path: Path) -> Cassette:
    if not path.exists():
        raise ValueError(f"{path}: no such cassette")
    proc = subprocess.run(["git", "show", f"{rev}:./{path.name}"], cwd=path.resolve().parent,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValueError(f"cannot read {path} at {rev}: {proc.stderr.strip() or 'git show failed'}")
    return Cassette.parse(proc.stdout, f"{rev}:{path}")


def cmd_diff(args: argparse.Namespace) -> int:
    fail_on = _kinds(args.fail_on, "--fail-on")
    ignore = _kinds(args.ignore, "--ignore")
    if args.new is None:
        old, new = _git_cassette(args.git, args.old), _load_existing(args.old)
    else:
        old, new = _load_existing(args.old), _load_existing(args.new)
    flags = DEFAULT_IGNORE_FLAGS + tuple(args.ignore_flag or ())
    result = diff_cassettes(old, new, pair_by=args.pair, tool_key=args.tool_key, ignore=ignore, ignore_flags=flags)
    failing = result.failing(fail_on)
    if args.json:
        print(json.dumps(dict(result.to_json(), failing=sorted(failing)), indent=2, ensure_ascii=False))
    else:
        print(result.render(verbose=args.verbose))
        if failing:
            print(f"tapedeck diff: failing on {', '.join(sorted(failing))}", file=sys.stderr)
    return 1 if failing else 0


def cmd_shim(args: argparse.Namespace) -> int:
    real = write_shims(args.dir.resolve(), args.bin or DEFAULT_BINS, os.environ.get("PATH", os.defpath))
    for name, path in real.items():
        print(f"# {name} -> {path or 'not found (replay only)'}", file=sys.stderr)
    print(f'export PATH="{args.dir.resolve()}:$PATH"')
    print('export TAPEDECK_CASSETTE="${TAPEDECK_CASSETTE:?set to your cassette path}"')
    print('export TAPEDECK_MODE="${TAPEDECK_MODE:-replay}"')
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    writes = {}
    for item in args.write:
        path, sep, content = item.partition("=")
        if not sep:
            raise ValueError(f"--write expects PATH=CONTENT, got {item!r}")
        writes[path] = content
    match: dict[str, object] = {"argv": "any", "stdin": "any"}
    if args.when_argv_contains:
        match["argv"] = {"contains": args.when_argv_contains}
    if args.when_stdin_contains:
        match["stdin"] = {"contains": args.when_stdin_contains}
    if args.fault:
        Fault.parse(args.fault, 1)
    rec = synth_interaction(args.program, args.result, fmt=args.format, exit_code=args.exit_code, writes=writes,
                            deletes=args.delete, match=match, fault=args.fault)
    with locked(args.cassette):
        cassette = Cassette.load(args.cassette)
        cassette.interactions.append(rec)
        cassette.save(args.cassette)
    print(f"tapedeck: appended interaction #{len(cassette.interactions) - 1} to {args.cassette}", file=sys.stderr)
    return 0


def _load_existing(path: Path) -> Cassette:
    if not path.exists():
        raise ValueError(f"{path}: no such cassette")
    return Cassette.load(path)


__all__ = ["main", "MISS_EXIT_CODE"]
