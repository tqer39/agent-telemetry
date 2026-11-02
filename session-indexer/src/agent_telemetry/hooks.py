"""Low-cost advisory hooks shared by Claude Code and Codex. Also executable directly."""

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

MAX_INPUT_BYTES = 2 * 1024 * 1024
COMPACT_TIP = (
    "圧縮後の引き継ぎ: 目的・制約、決定事項、変更ファイル、未完了項目、検証結果と次の一手を短く確認する。"
    "確認できない情報は推測で補わない。生ログ・全文を読み直さず、必要な箇所から再開する。"
)


def tip_for(payload, threshold=20_000):
    if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        return None
    event = payload.get("hook_event_name")
    if event == "SessionStart" and payload.get("source") == "compact":
        return "after_compact", {"hookSpecificOutput": {"hookEventName": event, "additionalContext": COMPACT_TIP}}
    if event != "PostToolUse" or "tool_response" not in payload:
        return None
    response = payload["tool_response"]
    # Count only the result, not commands or user input. Characters are not tokens.
    size = len(response) if isinstance(response, str) else len(json.dumps(response, ensure_ascii=False))
    if size < threshold:
        return None
    text = (
        f"圧縮ヒント: 今回のツール結果は約{size:,}文字（トークン数ではありません）。"
        "次回は検索対象・行範囲・出力件数を絞り、必要な結果と検証証拠を残す。"
        "会話が長くなったら目的・決定・未完了項目・検証結果を短く引き継いで圧縮や会話分割を検討する。"
        "このhookは元の結果を変更していません。"
    )
    # User-visible advice only: do not insert another reminder on every tool call.
    return "large_output", {"systemMessage": text}


def claim_notification(state_dir, session, category, cooldown, now):
    """An atomic SQLite write suppresses duplicates, including concurrent hooks."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((session + "\0" + category).encode()).hexdigest()
    with closing(sqlite3.connect(state_dir / "notifications.sqlite3", timeout=0.1)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, seen REAL NOT NULL)")
        with conn:
            # The predicate and replacement happen under the same write lock.
            cursor = conn.execute(
                """INSERT INTO notifications VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET seen=excluded.seen
                WHERE notifications.seen <= excluded.seen - ?""",
                (key, now, cooldown),
            )
            return cursor.rowcount == 1


def handle(payload, state_dir, *, threshold=20_000, cooldown=900, now=None):
    tip = tip_for(payload, threshold)
    if tip is None:
        return None
    category, result = tip
    try:
        if not claim_notification(Path(state_dir), payload["session_id"], category, cooldown, time.time() if now is None else now):
            return None
    except (OSError, sqlite3.Error):
        # Without persistent suppression, silence is preferable to repeated hints.
        return None
    return result


def configuration():
    parts = [sys.executable, str(Path(__file__).resolve())]
    command = subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)
    handler = {"type": "command", "command": command, "timeout": 3}
    return {
        "hooks": {
            "SessionStart": [{"matcher": "compact", "hooks": [dict(handler)]}],
            "PostToolUse": [{"hooks": [dict(handler)]}],
        }
    }


def main():
    parser = argparse.ArgumentParser(description="圧縮・引き継ぎの短いhookヒント（Claude Code / Codex）")
    parser.add_argument("--config", action="store_true", help="現在のPythonと絶対パスを使った登録用JSONを出力")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".cache/agent-telemetry/hook-tips")
    parser.add_argument("--threshold", type=int, default=20_000, help="ツール結果の文字数閾値")
    parser.add_argument("--cooldown", type=int, default=900, help="同一セッション・種類の通知間隔（秒）")
    args = parser.parse_args()
    if args.threshold < 1 or args.cooldown < 1:
        parser.error("threshold と cooldown は1以上が必要です")
    if args.config:
        print(json.dumps(configuration(), ensure_ascii=False, indent=2))
        return
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return
        payload = json.loads(raw)
        output = handle(payload, args.state_dir.expanduser(), threshold=args.threshold, cooldown=args.cooldown)
        if output:
            print(json.dumps(output, ensure_ascii=False))
    except (OSError, ValueError, RecursionError):
        # Fail open: never turn malformed telemetry into a blocked user operation.
        return


if __name__ == "__main__":
    main()
