import json
import os
import time
from pathlib import Path

import pytest

from session_indexer.claude import parse_claude_file
from session_indexer.codex import parse_codex_file
from session_indexer.indexer import Indexer
from session_indexer.model import normalize_command


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def claude_rows(session: str = "s-1") -> list[dict]:
    base = {"sessionId": session, "cwd": "/Users/t/src/my-repo"}
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}
    return [
        {"type": "summary", "summary": "no session id here"},
        {**base, "type": "user", "timestamp": "2026-10-01T00:00:00Z", "message": {"role": "user", "content": "run tests"}},
        # one API response split over two lines with the same usage → counted once
        {
            **base,
            "type": "assistant",
            "timestamp": "2026-10-01T00:00:10Z",
            "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage, "content": [{"type": "text", "text": "ok"}]},
        },
        {
            **base,
            "type": "assistant",
            "timestamp": "2026-10-01T00:00:11Z",
            "message": {
                "id": "m1",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest  -q"}}],
            },
        },
        {
            **base,
            "type": "user",
            "timestamp": "2026-10-01T00:00:20Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "1 failed"}]},
        },
        {
            **base,
            "type": "assistant",
            "timestamp": "2026-10-01T00:00:30Z",
            "message": {
                "id": "m2",
                "model": "claude-opus-5-5",
                "usage": usage,
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}},
                    {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "x"}},
                ],
            },
        },
        {
            **base,
            "type": "user",
            "timestamp": "2026-10-01T00:00:40Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t2", "content": "passed"},
                    {"type": "tool_result", "tool_use_id": "t3", "content": "..."},
                ]
            },
        },
        # an idle hour, then one more event: wall grows, active doesn't
        {**base, "type": "user", "timestamp": "2026-10-01T01:00:40Z", "message": {"content": "thanks"}},
        "not-a-dict",
    ]


def test_claude_parser(tmp_path: Path) -> None:
    rows = claude_rows()
    path = tmp_path / "s-1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken json\n", encoding="utf-8")
    s = parse_claude_file(path, idle_gap_seconds=300)
    assert s is not None
    assert (s.tool, s.session_id, s.project) == ("claude-code", "s-1", "my-repo")
    assert s.models == {"claude-opus-5-5"}
    assert (s.input_tokens, s.output_tokens, s.cache_read_tokens, s.cache_creation_tokens) == (20, 10, 160, 20)
    assert s.cache_hit_ratio == pytest.approx(160 / 200)
    assert (s.tool_calls, s.tool_failures, s.shell_commands) == (3, 1, 2)
    assert s.repeated_commands == 1  # whitespace-normalized "pytest -q"
    assert s.retries_after_failure == 1
    assert s.parse_errors == 1
    assert s.wall_seconds == 3640
    assert s.active_seconds == 40


def codex_rows() -> list[dict]:
    def item(ts: str, payload: dict, kind: str = "response_item") -> dict:
        return {"timestamp": ts, "type": kind, "payload": payload}

    return [
        item(
            "2026-10-02T00:00:00Z",
            {"id": "019a-uuid", "cwd": "/Users/t/src/other", "git": {"repository_url": "git@github.com:tqer39/agent-telemetry.git"}},
            "session_meta",
        ),
        item("2026-10-02T00:00:01Z", {"model": "gpt-5.5-codex"}, "turn_context"),
        item(
            "2026-10-02T00:00:05Z",
            {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["bash", "-lc", "make test"]}), "call_id": "c1"},
        ),
        item(
            "2026-10-02T00:00:09Z",
            {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"output": "boom", "metadata": {"exit_code": 2}})},
        ),
        item(
            "2026-10-02T00:00:20Z",
            {"type": "function_call", "name": "shell_command", "arguments": json.dumps({"command": "make test"}), "call_id": "c2"},
        ),
        item(
            "2026-10-02T00:00:25Z",
            {"type": "function_call_output", "call_id": "c2", "output": "Exit code: 0\nWall time: 1 seconds\nOutput:\nok"},
        ),
        item("2026-10-02T00:00:26Z", {"type": "exec_command_end", "call_id": "c2", "exit_code": 0}, "event_msg"),
        item("2026-10-02T00:00:27Z", {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch", "call_id": "c3"}),
        item("2026-10-02T00:00:28Z", {"type": "token_count", "info": None}, "event_msg"),
        item(
            "2026-10-02T00:00:29Z",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 600,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 20,
                    }
                },
            },
            "event_msg",
        ),
    ]


def test_codex_parser(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "2026/10/02/rollout-2026-10-02T00-00-00-019a-uuid.jsonl", codex_rows())
    s = parse_codex_file(path, idle_gap_seconds=300)
    assert s is not None
    assert (s.tool, s.session_id, s.project) == ("codex", "019a-uuid", "agent-telemetry")
    assert s.models == {"gpt-5.5-codex"}
    assert (s.input_tokens, s.cache_read_tokens, s.output_tokens, s.reasoning_tokens) == (400, 600, 50, 20)
    assert s.cache_hit_ratio == pytest.approx(0.6)
    assert (s.tool_calls, s.shell_commands, s.tool_failures) == (3, 2, 1)
    assert s.repeated_commands == 1 and s.retries_after_failure == 1


def test_claude_worktree_cwd_maps_to_repo(tmp_path: Path) -> None:
    rows = claude_rows()
    for r in rows:
        if isinstance(r, dict) and "cwd" in r:
            r["cwd"] = "/Users/t/src/my-repo/.claude/worktrees/fix-bug-1a2b3c"
    s = parse_claude_file(write_jsonl(tmp_path / "s-1.jsonl", rows), idle_gap_seconds=300)
    assert s is not None and s.project == "my-repo"


def codex_desktop_rows(parent: str | None = "019a-parent") -> list[dict]:
    """Shape written by Codex Desktop (2026-10): the model calls `exec`/`js`, and each
    command / MCP call it runs is reported as an `item_completed` event with a status."""

    def item(ts: str, payload: dict, kind: str = "response_item") -> dict:
        return {"timestamp": ts, "type": kind, "payload": payload}

    def done(ts: str, it: dict) -> dict:
        return item(ts, {"type": "item_completed", "item": it}, "event_msg")

    meta = {"id": "019a-sub", "cwd": "/Users/t/src/my-repo", "originator": "Codex Desktop"}
    if parent:
        meta["parent_thread_id"] = parent
    return [
        item("2026-10-03T00:00:00Z", meta, "session_meta"),
        item("2026-10-03T00:00:01Z", {"type": "custom_tool_call", "name": "exec", "input": "...", "call_id": "c1"}),
        done(
            "2026-10-03T00:00:05Z",
            {"type": "CommandExecution", "id": "i1", "command": ["/bin/zsh", "-lc", "make test"], "status": "failed", "exit_code": 1},
        ),
        item(
            "2026-10-03T00:00:06Z", {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "..."}]}
        ),
        item("2026-10-03T00:00:07Z", {"type": "custom_tool_call", "name": "exec", "input": "...", "call_id": "c2"}),
        done(
            "2026-10-03T00:00:09Z",
            {
                "type": "CommandExecution",
                "id": "i2",
                "command": ["/opt/homebrew/bin/bash", "-c", "make  test"],
                "status": "completed",
                "exit_code": 0,
            },
        ),
        item("2026-10-03T00:00:10Z", {"type": "function_call", "name": "js", "arguments": "{}", "call_id": "c3"}),
        done("2026-10-03T00:00:11Z", {"type": "McpToolCall", "id": "i3", "server": "s", "tool": "t", "status": "failed"}),
        done("2026-10-03T00:00:12Z", {"type": "FileChange", "id": "i4", "status": "completed"}),
        done("2026-10-03T00:00:13Z", {"type": "AgentMessage", "id": "i5"}),
        item(
            "2026-10-03T00:00:14Z",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 600,
                        "cache_write_input_tokens": 100,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 20,
                    }
                },
            },
            "event_msg",
        ),
    ]


def test_codex_desktop_items(tmp_path: Path) -> None:
    s = parse_codex_file(write_jsonl(tmp_path / "rollout-a.jsonl", codex_desktop_rows()), idle_gap_seconds=300)
    assert s is not None
    assert (s.session_id, s.project) == ("019a-parent", "my-repo")  # subagent rolls up into its parent
    assert (s.input_tokens, s.cache_read_tokens, s.cache_creation_tokens) == (300, 600, 100)
    # executed items, not the exec/js wrappers around them
    assert (s.tool_calls, s.shell_commands, s.tool_failures) == (4, 2, 2)
    assert s.repeated_commands == 1 and s.retries_after_failure == 1


def test_codex_empty_subagent_stub_is_skipped(tmp_path: Path) -> None:
    rows = [
        {"timestamp": "2026-10-03T00:00:00Z", "type": "session_meta", "payload": {"id": "x", "parent_thread_id": "p"}},
        {"timestamp": "2026-10-03T00:00:00Z", "type": "response_item", "payload": {"type": "compaction"}},
    ]
    assert parse_codex_file(write_jsonl(tmp_path / "rollout-b.jsonl", rows), idle_gap_seconds=300) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["bash", "-lc", "ls  -la"], "ls -la"),
        (["/opt/homebrew/bin/bash", "-c", "ls"], "ls"),
        (["git", "commit", "-m", "a b"], "git commit -m 'a b'"),
        ("  echo   hi ", "echo hi"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_command(raw: object, expected: str | None) -> None:
    assert normalize_command(raw) == expected


def test_indexer_merges_subagent_files_and_respects_lookback(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    write_jsonl(claude_root / "proj/s-1.jsonl", claude_rows("s-1"))
    write_jsonl(claude_root / "proj/s-1/subagents/agent-a.jsonl", claude_rows("s-1"))  # same session id
    old = write_jsonl(claude_root / "proj/s-old.jsonl", claude_rows("s-old"))
    week_ago = time.time() - 30 * 86400
    os.utime(old, (week_ago, week_ago))
    write_jsonl(codex_root / "2026/10/02/rollout-x.jsonl", codex_rows())

    idx = Indexer(claude_root, codex_root, lookback_days=14, idle_gap_seconds=300)
    sessions = idx.scan()
    by_key = {(s.tool, s.session_id): s for s in sessions}
    assert set(by_key) == {("claude-code", "s-1"), ("codex", "019a-uuid")}
    merged = by_key[("claude-code", "s-1")]
    assert merged.tool_calls == 6 and merged.input_tokens == 40

    # rescanning unchanged files must not double count (cached results are not mutated)
    again = {(s.tool, s.session_id): s for s in idx.scan()}
    assert again[("claude-code", "s-1")].tool_calls == 6
    assert idx.stats.files_parsed == 0


def test_indexer_handles_missing_dirs(tmp_path: Path) -> None:
    idx = Indexer(tmp_path / "nope", tmp_path / "nope2", lookback_days=1, idle_gap_seconds=300)
    assert idx.scan() == []
    assert set(idx.stats.sources_missing) == {"claude-code", "codex"}
