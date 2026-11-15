"""Parser for Claude Code transcripts: ~/.claude/projects/<project>/<session>.jsonl.

Written by Claude Code in the terminal and by the Code feature of the Claude Desktop app.
The format is undocumented; every field access is defensive.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import SessionSummary, normalize_command, parse_iso, project_from_cwd

TOOL = "claude-code"
_SHELL_TOOLS = {"Bash"}


def parse_claude_file(path: Path, idle_gap_seconds: float) -> SessionSummary | None:
    summary: SessionSummary | None = None
    seen_message_ids: set[str] = set()
    tool_commands: dict[str, str | None] = {}

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                if summary is not None:
                    summary.parse_errors += 1
                continue
            if not isinstance(entry, dict):
                continue

            session_id = entry.get("sessionId")
            if summary is None:
                if not isinstance(session_id, str):
                    continue  # e.g. leading "summary" lines without a session id
                summary = SessionSummary(tool=TOOL, session_id=session_id)
            if summary.project == "unknown" and isinstance(entry.get("cwd"), str):
                summary.project = project_from_cwd(entry["cwd"])

            summary.observe_ts(parse_iso(entry.get("timestamp")))
            message = entry.get("message")
            if not isinstance(message, dict):
                continue

            if entry.get("type") == "assistant":
                _handle_assistant(summary, message, seen_message_ids, tool_commands)
            elif entry.get("type") == "user":
                _handle_user(summary, message, tool_commands)

    return summary.finalize(idle_gap_seconds) if summary else None


def _handle_assistant(
    summary: SessionSummary,
    message: dict,
    seen_message_ids: set[str],
    tool_commands: dict[str, str | None],
) -> None:
    if isinstance(message.get("model"), str) and not message["model"].startswith("<"):
        summary.models.add(message["model"])

    # One API response is split across several lines that repeat the same usage block.
    msg_id = message.get("id")
    usage = message.get("usage")
    if isinstance(usage, dict) and (not isinstance(msg_id, str) or msg_id not in seen_message_ids):
        if isinstance(msg_id, str):
            seen_message_ids.add(msg_id)
        summary.input_tokens += _int(usage.get("input_tokens"))
        summary.output_tokens += _int(usage.get("output_tokens"))
        summary.cache_read_tokens += _int(usage.get("cache_read_input_tokens"))
        summary.cache_creation_tokens += _int(usage.get("cache_creation_input_tokens"))

    for block in _blocks(message):
        if block.get("type") != "tool_use":
            continue
        summary.tool_calls += 1
        command = None
        if block.get("name") in _SHELL_TOOLS and isinstance(block.get("input"), dict):
            command = normalize_command(block["input"].get("command"))
            summary.record_shell_command(command)
        if isinstance(block.get("id"), str):
            tool_commands[block["id"]] = command


def _handle_user(summary: SessionSummary, message: dict, tool_commands: dict[str, str | None]) -> None:
    for block in _blocks(message):
        if block.get("type") != "tool_result":
            continue
        command = tool_commands.get(block.get("tool_use_id", ""))
        summary.record_result(command, failed=block.get("is_error") is True)


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _int(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0
