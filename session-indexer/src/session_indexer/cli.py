"""session-indexer: summarize Claude Code / Codex transcripts.

serve   expose Prometheus metrics (used by compose)
report  print a per-session table locally, no Docker needed
schema  print the key structure of a JSONL file (keys and types only, no values)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from .indexer import Indexer

log = logging.getLogger("session_indexer")


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _build_indexer(args: argparse.Namespace) -> Indexer:
    return Indexer(
        claude_root=None if args.no_claude else _env_path("CLAUDE_PROJECTS_DIR", "~/.claude/projects"),
        codex_root=None if args.no_codex else _env_path("CODEX_SESSIONS_DIR", "~/.codex/sessions"),
        lookback_days=float(args.days if args.days is not None else os.environ.get("LOOKBACK_DAYS", "14")),
        idle_gap_seconds=float(os.environ.get("IDLE_GAP_SECONDS", "300")),
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from prometheus_client import REGISTRY, start_http_server

    from .metrics import SessionCollector

    indexer = _build_indexer(args)
    indexer.scan()
    REGISTRY.register(SessionCollector(indexer))
    start_http_server(args.port, addr=args.addr)
    log.info("serving on %s:%d, %d sessions", args.addr, args.port, len(indexer.sessions))
    interval = float(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                indexer.scan()
            except Exception:  # keep serving the last good snapshot
                log.exception("scan failed")

    threading.Thread(target=loop, daemon=True).start()
    threading.Event().wait()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    sessions = _build_indexer(args).scan()
    if args.json:
        rows = [
            {
                "tool": s.tool,
                "session_id": s.session_id,
                "project": s.project,
                "started_at": s.first_ts.isoformat() if s.first_ts else None,
                "wall_seconds": round(s.wall_seconds),
                "active_seconds": round(s.active_seconds),
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_creation_tokens": s.cache_creation_tokens,
                "cache_hit_ratio": round(s.cache_hit_ratio, 3),
                "tool_calls": s.tool_calls,
                "tool_failures": s.tool_failures,
                "shell_commands": s.shell_commands,
                "repeated_commands": s.repeated_commands,
                "retries_after_failure": s.retries_after_failure,
            }
            for s in sessions
        ]
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    header = (
        f"{'tool':<11} {'project':<22} {'start':<16} {'active':>7} {'tokens':>10} "
        f"{'cache%':>6} {'calls':>5} {'fail':>4} {'rep':>4} {'retry':>5}"
    )
    print(header)
    print("-" * len(header))
    for s in sessions:
        start = s.first_ts.strftime("%m-%d %H:%M") if s.first_ts else "-"
        total = s.total_input_tokens + s.output_tokens
        print(
            f"{s.tool:<11} {s.project[:22]:<22} {start:<16} {_dur(s.active_seconds):>7} {total:>10,} "
            f"{s.cache_hit_ratio * 100:>5.0f}% {s.tool_calls:>5} {s.tool_failures:>4} "
            f"{s.repeated_commands:>4} {s.retries_after_failure:>5}"
        )
    print(f"\n{len(sessions)} sessions (active = time excluding gaps > IDLE_GAP_SECONDS)")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Print key paths + value types so parsers can be checked without exposing content."""
    counts: Counter[str] = Counter()
    with Path(args.file).expanduser().open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                _walk(json.loads(raw), "$", counts, depth=0)
            except json.JSONDecodeError:
                counts["<invalid json line>"] += 1
    for key, n in sorted(counts.items()):
        print(f"{n:>6}  {key}")
    return 0


def _walk(value: object, path: str, counts: Counter[str], depth: int) -> None:
    if depth > 6:
        return
    if isinstance(value, dict):
        # "type" values are enums (e.g. tool_use / token_count), safe and useful to show
        tag = value.get("type")
        if isinstance(tag, str) and len(tag) < 40:
            counts[f"{path}.type = {tag}"] += 1
        for k, v in value.items():
            _walk(v, f"{path}.{k}", counts, depth + 1)
    elif isinstance(value, list):
        for v in value[:50]:
            _walk(v, f"{path}[]", counts, depth + 1)
    else:
        counts[f"{path}: {type(value).__name__}"] += 1


def _dur(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="session-indexer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--days", type=float, default=None, help="lookback window (default: LOOKBACK_DAYS or 14)")
        p.add_argument("--no-claude", action="store_true")
        p.add_argument("--no-codex", action="store_true")

    p_serve = sub.add_parser("serve")
    common(p_serve)
    p_serve.add_argument("--addr", default=os.environ.get("LISTEN_ADDR", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("LISTEN_PORT", "9464")))

    p_report = sub.add_parser("report")
    common(p_report)
    p_report.add_argument("--json", action="store_true")

    p_schema = sub.add_parser("schema")
    p_schema.add_argument("file")

    args = parser.parse_args(argv)
    handlers = {"serve": cmd_serve, "report": cmd_report, "schema": cmd_schema}
    if args.cmd not in handlers:
        parser.print_help()
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
