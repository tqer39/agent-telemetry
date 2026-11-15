"""Parser for Codex rollout files: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Written by both the Codex CLI and the Codex Desktop app. Each line is
`{"timestamp", "type", "payload"}`. The schema changes between versions, so
several known shapes are accepted and anything else is ignored.

Codex Desktop (2026-10) wraps tool use in `exec`/`js` calls and reports each command
or MCP call it runs as an `item_completed` event; when those exist they are counted
instead of the wrappers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePath

from .model import SessionSummary, normalize_command, parse_iso, project_from_cwd

TOOL = "codex"
_SHELL_FUNCTIONS = {"shell", "shell_command", "exec_command", "container.exec", "local_shell"}
_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call"}
_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}
_ITEM_TOOLS = {"CommandExecution", "McpToolCall", "FileChange", "ImageView", "CollabAgentToolCall", "WebSearch"}
_EXIT_RE = re.compile(r"(?:exit code|exited with code)[:\s]+(-?\d+)", re.IGNORECASE)


def parse_codex_file(path: Path, idle_gap_seconds: float) -> SessionSummary | None:
    summary = SessionSummary(tool=TOOL, session_id=_session_id_from_name(path))
    call_commands: dict[str, str | None] = {}
    resolved_calls: set[str] = set()
    last_usage: dict | None = None
    item_calls = 0

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                summary.parse_errors += 1
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            summary.observe_ts(parse_iso(entry.get("timestamp")))
            kind = entry.get("type")

            if kind == "session_meta":
                _handle_meta(summary, payload)
            elif kind == "turn_context":
                if isinstance(payload.get("model"), str):
                    summary.models.add(payload["model"])
            elif kind == "response_item":
                _handle_item(summary, payload, call_commands, resolved_calls)
            elif kind == "event_msg":
                ptype = payload.get("type")
                if ptype == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                        last_usage = info["total_token_usage"]  # cumulative: keep the last one
                elif ptype == "item_completed" and isinstance(payload.get("item"), dict):
                    item_calls += _handle_completed(summary, payload["item"])
                elif ptype == "exec_command_end":
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and call_id not in resolved_calls:
                        resolved_calls.add(call_id)
                        code = payload.get("exit_code")
                        summary.record_result(call_commands.get(call_id), failed=isinstance(code, int) and code != 0)

    if item_calls:
        summary.tool_calls = item_calls
    if not last_usage and not summary.tool_calls:
        return None  # e.g. Desktop subagent stubs that only hold a compaction
    if last_usage:
        total_input = _int(last_usage.get("input_tokens"))
        cached = min(_int(last_usage.get("cached_input_tokens")), total_input)
        written = min(_int(last_usage.get("cache_write_input_tokens")), total_input - cached)
        summary.input_tokens = total_input - cached - written  # OpenAI counts cache tokens inside input
        summary.cache_read_tokens = cached
        summary.cache_creation_tokens = written
        summary.output_tokens = _int(last_usage.get("output_tokens"))
        summary.reasoning_tokens = _int(last_usage.get("reasoning_output_tokens"))
    return summary.finalize(idle_gap_seconds)


def _handle_meta(summary: SessionSummary, payload: dict) -> None:
    # subagent threads roll up into the thread that spawned them, like Claude subagents
    for key in ("parent_thread_id", "id"):
        if isinstance(payload.get(key), str) and payload[key]:
            summary.session_id = payload[key]
            break
    git = payload.get("git")
    repo_url = git.get("repository_url") if isinstance(git, dict) else None
    if isinstance(repo_url, str) and repo_url:
        summary.project = PurePath(repo_url.rstrip("/")).name.removesuffix(".git")
    elif isinstance(payload.get("cwd"), str):
        summary.project = project_from_cwd(payload["cwd"])


def _handle_completed(summary: SessionSummary, item: dict) -> int:
    """Count one executed tool item; returns 1 if it is a tool item, else 0."""
    if item.get("type") not in _ITEM_TOOLS:
        return 0
    command = None
    if item.get("type") == "CommandExecution":
        command = normalize_command(item.get("command"))
        summary.record_shell_command(command)
    code = item.get("exit_code")
    failed = item.get("status") == "failed" or (isinstance(code, int) and code != 0)
    summary.record_result(command, failed=failed)
    return 1


def _handle_item(
    summary: SessionSummary,
    payload: dict,
    call_commands: dict[str, str | None],
    resolved_calls: set[str],
) -> None:
    ptype = payload.get("type")
    call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None

    if ptype in _CALL_TYPES:
        summary.tool_calls += 1
        command = _extract_command(payload)
        if command is not None:
            summary.record_shell_command(command)
        if call_id:
            call_commands[call_id] = command
    elif ptype in _OUTPUT_TYPES and call_id and call_id not in resolved_calls:
        failed = _output_failed(payload.get("output"))
        if failed is not None:
            resolved_calls.add(call_id)
            summary.record_result(call_commands.get(call_id), failed=failed)


def _extract_command(payload: dict) -> str | None:
    if payload.get("type") == "local_shell_call":
        action = payload.get("action")
        return normalize_command(action.get("command")) if isinstance(action, dict) else None
    if payload.get("name") not in _SHELL_FUNCTIONS:
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return normalize_command(args)
    if isinstance(args, dict):
        return normalize_command(args.get("command", args.get("cmd")))
    return None


def _output_failed(output: object) -> bool | None:
    """True/False when the exit status is recognizable, None when it isn't."""
    if isinstance(output, dict):
        meta = output.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
            return meta["exit_code"] != 0
        output = output.get("output")
    if not isinstance(output, str):
        return None
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        meta = decoded.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int):
            return meta["exit_code"] != 0
    match = _EXIT_RE.search(output[:300])
    return int(match.group(1)) != 0 if match else None


def _session_id_from_name(path: Path) -> str:
    # rollout-2026-04-29T08-14-22-<uuid>.jsonl → keep the uuid-ish tail
    stem = path.stem
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) > 5 else stem


def _int(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0
