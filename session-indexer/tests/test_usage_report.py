import contextlib
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent_telemetry.__main__ import main
from agent_telemetry.logs import zstd
from agent_telemetry.report import render
from agent_telemetry.usage import scan


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.now = datetime(2026, 10, 31, 8, tzinfo=UTC)

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def report(self, days=30):
        return scan(self.claude, self.codex, days, ZoneInfo("Asia/Tokyo"), self.now)

    def claude_row(self, output=10):
        return {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-10-30T16:00:00Z",
            "message": {
                "id": "m1",
                "model": "claude-test",
                "content": "SECRET PROMPT",
                "usage": {"input_tokens": 5, "output_tokens": output, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
            },
        }

    def legacy(self, input_count, output=10):
        return {
            "type": "event_msg",
            "timestamp": "2026-10-31T00:00:00Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": input_count, "output_tokens": output, "cached_input_tokens": input_count // 2}
                },
            },
        }

    def test_timezone_without_system_database(self):
        import zoneinfo

        original = zoneinfo.TZPATH
        try:
            zoneinfo.reset_tzpath([])
            zone = ZoneInfo.no_cache("Asia/Tokyo")
            self.assertEqual(self.now.astimezone(zone).hour, 17)
        finally:
            zoneinfo.reset_tzpath(original)

    def test_multiple_claude_sessions_partition_activity_and_trace(self):
        rows = []
        for sid, amount, calls in (("first", 10, 1), ("second", 20, 2)):
            row = self.claude_row(amount)
            row["sessionId"] = sid
            row["cwd"] = f"/missing/{sid}"
            row["message"]["content"] = [{"type": "tool_use", "id": str(i), "name": "read", "input": {}} for i in range(calls)]
            rows.append(row)
        rows.append({"type": "user", "sessionId": "empty", "timestamp": rows[0]["timestamp"]})
        self.write(self.claude / "mixed.jsonl", rows)
        report = self.report()
        sessions = {s["session_id"]: s for s in report["sessions"]}
        self.assertEqual(set(sessions), {"first", "second", "empty"})
        self.assertEqual([sessions[s]["tool_calls"] for s in ("first", "second", "empty")], [1, 2, 0])
        self.assertEqual([sessions[s]["output"] for s in ("first", "second", "empty")], [10, 20, 0])
        self.assertEqual(report["providers"]["claude"]["sessions_without_usage"], 1)
        self.assertEqual(sum(s["input"] for s in sessions.values()), report["providers"]["claude"]["input"])
        self.assertEqual(sessions["first"]["cwd"], "/missing/first")

    def test_codex_sessions_have_independent_counters_and_format_selection(self):
        rows = []
        for sid, amount in (("first", 100), ("second", 50)):
            rows.extend([{"type": "session_meta", "payload": {"id": sid}}, self.legacy(amount)])
        rows.append(
            {
                "type": "token_usage_record",
                "timestamp": "2026-10-31T00:00:00Z",
                "payload": {"response_id": "r1", "usage": {"input_tokens": 70}},
            }
        )
        self.write(self.codex / "sessions/mixed.jsonl", rows)
        report = self.report()
        sessions = {s["session_id"]: s for s in report["sessions"]}
        self.assertEqual({sid: s["input"] for sid, s in sessions.items()}, {"first": 100, "second": 70})
        self.assertEqual(report["providers"]["codex"]["input"], 170)

    def test_malformed_compaction_uuid_does_not_abort_scan(self):
        rows = [self.claude_row()]
        for uuid in ([], {"private": "SECRET"}):
            rows.append(
                {"type": "system", "subtype": "compact_boundary", "uuid": uuid, "sessionId": "s1", "timestamp": rows[0]["timestamp"]}
            )
        self.write(self.claude / "malformed.jsonl", rows)
        report = self.report()
        self.assertEqual(report["sessions"][0]["compactions"], 2)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_claude_streaming_and_copied_logs_count_once(self):
        rows = [self.claude_row(), self.claude_row(30)]
        self.write(self.claude / "p/s.jsonl", rows)
        self.write(self.claude / "p/copy.jsonl", rows)
        result = self.report()
        c = result["providers"]["claude"]
        self.assertEqual((c["input"], c["output"], c["requests"], c["sessions"]), (125, 30, 1, 1))
        self.assertNotIn("SECRET PROMPT", json.dumps(result))
        self.assertNotIn("SECRET PROMPT", render(result))
        self.assertEqual(result["daily"][0]["date"], "2026-10-31")

    def test_legacy_cumulative_duplicates_and_rewind(self):
        meta = {"type": "session_meta", "payload": {"id": "s1"}}
        rows = [meta, self.legacy(100), self.legacy(100), self.legacy(80), self.legacy(150, 20)]
        self.write(self.codex / "sessions/a.jsonl", rows)
        self.write(self.codex / "archived_sessions/a.jsonl", rows)
        c = self.report()["providers"]["codex"]
        self.assertEqual((c["input"], c["output"], c["requests"], c["cache_read"]), (150, 20, 2, 75))

    def test_new_records_preferred_to_duplicated_legacy(self):
        modern = {
            "type": "token_usage_record",
            "timestamp": "2026-10-31T00:00:00Z",
            "payload": {
                "response_id": "r1",
                "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 70, "reasoning_output_tokens": 5},
            },
        }
        rows = [{"type": "turn_context", "payload": {"model": "codex-test"}}, self.legacy(100, 20), modern, modern]
        self.write(self.codex / "sessions/a.jsonl", rows)
        c = self.report()["providers"]["codex"]
        self.assertEqual((c["input"], c["output"], c["requests"], c["reasoning"]), (100, 20, 1, 5))

    def test_period_uses_deltas_and_local_dates(self):
        old = self.legacy(100)
        old["timestamp"] = "2026-10-29T12:00:00Z"
        self.write(self.codex / "sessions/a.jsonl", [old, self.legacy(140, 20)])
        self.write(self.claude / "s.jsonl", [self.claude_row()])
        result = self.report(days=1)
        self.assertEqual(result["providers"]["codex"]["input"], 40)
        self.assertEqual(result["providers"]["claude"]["requests"], 1)

    def test_missing_malformed_partial_and_unknown_usage(self):
        self.assertFalse(any(s["found"] for s in self.report()["sources"]))
        path = self.claude / "a.jsonl"
        self.write(path, [None, {"type": "user", "timestamp": "2026-10-31T00:00:00Z", "message": "PRIVATE"}, self.claude_row()])
        with path.open("a") as stream:
            stream.write('{"unfinished":')
        self.write(
            self.codex / "sessions/a.jsonl", [{"type": "session_meta", "timestamp": "2026-10-31T00:00:00Z", "payload": {"id": "empty"}}]
        )
        (self.codex / "sessions/old.jsonl.unsupported").touch()
        result = self.report()
        self.assertEqual(result["diagnostics"]["invalid_lines"], 2)
        self.assertEqual(result["diagnostics"]["unsupported_files"], 1)
        self.assertEqual(result["providers"]["codex"]["sessions_without_usage"], 1)
        self.assertNotIn("PRIVATE", render(result))

    def test_gzip_history_and_period_filter(self):
        row = self.claude_row()
        row["timestamp"] = "2026-08-15T12:00:00Z"
        path = self.claude / "old.jsonl.gz"
        path.parent.mkdir(parents=True)
        original = gzip.compress((json.dumps(row) + "\n").encode())
        path.write_bytes(original)
        self.assertEqual(self.report()["providers"]["claude"]["input"], 0)
        full = self.report(days=0)
        self.assertEqual(full["providers"]["claude"]["input"], 125)
        self.assertEqual(full["daily"][0]["date"], "2026-08-15")
        self.assertEqual(full["sources"][0]["compressed_files"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("SECRET PROMPT", render(full))

    @unittest.skipUnless(zstd, "compression.zstd is not available")
    def test_zstd_history_for_both_providers_and_concatenated_frames(self):
        row = self.claude_row()
        row["timestamp"] = "2026-08-15T12:00:00Z"
        c = self.claude / "old.jsonl.zst"
        c.parent.mkdir(parents=True)
        c.write_bytes(zstd.compress((json.dumps(row) + "\n").encode()))
        old = self.legacy(100)
        old["timestamp"] = "2026-08-15T12:00:00Z"
        meta = {"type": "session_meta", "payload": {"id": "historical"}}
        a = self.codex / "archived_sessions/old.jsonl.zst"
        a.parent.mkdir(parents=True)
        a.write_bytes(zstd.compress((json.dumps(meta) + "\n").encode()) + zstd.compress((json.dumps(old) + "\n").encode()))
        original = a.read_bytes()
        full = self.report(days=0)
        self.assertEqual(full["providers"]["claude"]["input"], 125)
        self.assertEqual(full["providers"]["codex"]["input"], 100)
        self.assertEqual(full["providers"]["codex"]["sessions"], 1)
        self.assertEqual([s["compressed_files"] for s in full["sources"]], [1, 1])
        self.assertEqual(self.report()["daily"], [])
        self.assertEqual(a.read_bytes(), original)
        self.assertFalse(a.with_suffix("").exists())

    @unittest.skipUnless(zstd, "compression.zstd is not available")
    def test_plaintext_preferred_over_compressed_snapshot_without_ids(self):
        older = self.legacy(100)
        self.write(self.codex / "sessions/a.jsonl", [older, self.legacy(150, 20)])
        path = self.codex / "sessions/a.jsonl.zst"
        path.write_bytes(zstd.compress((json.dumps(older) + "\n").encode()))
        report = self.report()
        c = report["providers"]["codex"]
        self.assertEqual((c["input"], c["output"], c["requests"], c["sessions"]), (150, 20, 2, 1))
        self.assertEqual(report["diagnostics"]["duplicate_files"], 1)
        self.assertEqual(report["sources"][1]["files"], 1)
        self.assertEqual(report["sources"][1]["compressed_files"], 0)

    @unittest.skipUnless(zstd, "compression.zstd is not available")
    def test_compressed_copy_in_archive_not_double_counted(self):
        meta = {"type": "session_meta", "payload": {"id": "same"}}
        rows = [meta, self.legacy(100), self.legacy(150, 20)]
        self.write(self.codex / "sessions/a.jsonl", rows)
        path = self.codex / "archived_sessions/a.jsonl.zst"
        path.parent.mkdir(parents=True)
        path.write_bytes(zstd.compress(("\n".join(json.dumps(r) for r in rows) + "\n").encode()))
        c = self.report()["providers"]["codex"]
        self.assertEqual((c["input"], c["output"], c["requests"], c["sessions"]), (150, 20, 2, 1))

    def test_bad_gzip_does_not_stop_other_logs(self):
        self.write(self.claude / "good.jsonl", [self.claude_row()])
        (self.claude / "bad.jsonl.gz").write_bytes(b"not gzip")
        result = self.report()
        self.assertEqual(result["providers"]["claude"]["input"], 125)
        self.assertEqual(result["diagnostics"]["corrupt_compressed_files"], 1)

    @unittest.skipUnless(zstd, "compression.zstd is not available")
    def test_truncated_compression_discards_partial_usage(self):
        row = self.claude_row()
        row["message"]["content"] = "private" * 3000
        payload = (json.dumps(row) + "\n").encode()
        self.claude.mkdir()
        # A complete frame followed by a truncated frame must not contribute usage.
        for suffix, compress in (("zst", zstd.compress), ("gz", gzip.compress)):
            (self.claude / f"broken-{suffix}.jsonl.{suffix}").write_bytes(compress(payload) + compress(b"{}\n")[:-2])
        (self.claude / "bad.jsonl.zst").write_bytes(b"not zstd")
        report = self.report()
        self.assertEqual(report["providers"]["claude"]["requests"], 0)
        self.assertEqual(report["providers"]["claude"]["sessions"], 0)
        self.assertEqual(report["diagnostics"]["corrupt_compressed_files"], 3)

    def test_zstd_unavailable_is_actionable_and_plain_logs_still_work(self):
        self.write(self.claude / "a.jsonl", [self.claude_row()])
        path = self.codex / "sessions/old.jsonl.zst"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"placeholder")
        with patch("agent_telemetry.logs.zstd", None):
            report = self.report()
        self.assertEqual(report["providers"]["claude"]["input"], 125)
        self.assertEqual(report["diagnostics"]["zstd_unavailable_files"], 1)
        self.assertIn("Python 3.14", render(report))

    @unittest.skipUnless(zstd, "compression.zstd is not available")
    def test_corrupt_gzip_falls_back_to_zstd(self):
        path = self.claude / "a.jsonl.gz"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"broken")
        path.with_suffix(".zst").write_bytes(zstd.compress((json.dumps(self.claude_row()) + "\n").encode()))
        report = self.report()
        self.assertEqual(report["providers"]["claude"]["input"], 125)
        self.assertEqual(report["diagnostics"]["corrupt_compressed_files"], 1)
        self.assertEqual(report["sources"][0]["read_files"], 1)

    def test_model_html_is_escaped(self):
        row = self.claude_row()
        row["message"]["model"] = '<script>alert("bad")</script>'
        self.write(self.claude / "a.jsonl", [row])
        html = render(self.report())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_future_undated_and_invalid_token_values(self):
        undated = self.claude_row()
        undated.pop("timestamp")
        future = self.claude_row()
        future["message"]["id"] = "future"
        future["timestamp"] = "2026-11-02T00:00:00Z"
        valid = self.claude_row()
        valid["message"]["usage"] = {"input_tokens": -10, "output_tokens": "100", "cache_read_input_tokens": True}
        self.write(self.claude / "a.jsonl", [undated, future, valid])
        result = self.report()
        self.assertEqual(result["diagnostics"]["usage_without_timestamp"], 1)
        self.assertEqual(result["providers"]["claude"]["requests"], 1)
        self.assertEqual(result["providers"]["claude"]["input"], 0)
        self.assertEqual(result["providers"]["claude"]["output"], 0)

    def test_cli_json_and_protected_sources(self):
        self.write(self.claude / "a.jsonl", [self.claude_row()])
        command = [sys.executable, "-m", "agent_telemetry", "--claude-dir", str(self.claude), "--codex-dir", str(self.codex)]
        result = subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
            [*command, "--json"], capture_output=True, text=True, check=True
        )
        self.assertIn("providers", json.loads(result.stdout))
        original = (self.claude / "a.jsonl").read_bytes()
        rejected = subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
            [*command, "--output", str(self.claude / "a.jsonl")], capture_output=True
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual((self.claude / "a.jsonl").read_bytes(), original)
        # A Codex worktree may itself live below CODEX_HOME.
        output = self.codex / "worktrees/report.html"
        output.parent.mkdir(parents=True)
        subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
            [*command, "--output", str(output)], capture_output=True, check=True
        )
        self.assertIn("<!doctype html>", output.read_text())
        protected = subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
            [*command, "--output", str(self.codex / "sessions/report.html")], capture_output=True
        )
        self.assertNotEqual(protected.returncode, 0)
        for flags in (["--days", "-1"], ["--timezone", "wrong/zone"], ["--json", "--open"]):
            self.assertNotEqual(
                subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
                    [*command, *flags], capture_output=True
                ).returncode,
                0,
            )

    def test_output_hardlink_cannot_modify_source_transcript(self):
        source = self.claude / "a.jsonl"
        self.write(source, [self.claude_row()])
        original = source.read_bytes()
        output = self.root / "report.html"
        os.link(source, output)
        subprocess.run(  # noqa: S603 -- fixed CLI and synthetic paths without a shell
            [
                sys.executable,
                "-m",
                "agent_telemetry",
                "--claude-dir",
                str(self.claude),
                "--codex-dir",
                str(self.codex),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=True,
        )
        self.assertEqual(source.read_bytes(), original)
        self.assertIn("<!doctype html>", output.read_text())

    def test_failed_output_replacement_preserves_existing_report(self):
        self.write(self.claude / "a.jsonl", [self.claude_row()])
        output = self.root / "report.html"
        output.write_text("existing report")
        before = set(self.root.iterdir())
        args = ["agent-telemetry", "--claude-dir", str(self.claude), "--codex-dir", str(self.codex), "--output", str(output)]
        with (
            patch.object(sys, "argv", args),
            patch("agent_telemetry.__main__.os.replace", side_effect=PermissionError("blocked")),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main()
        self.assertEqual(output.read_text(), "existing report")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_malformed_identity_metadata_is_not_serialized_as_content(self):
        row = self.claude_row()
        row["sessionId"] = {"body": "PRIVATE SESSION CONTENT"}
        row["message"]["model"] = {"body": "PRIVATE MODEL CONTENT"}
        row["message"]["id"] = {"body": "PRIVATE RESPONSE CONTENT"}
        self.write(self.claude / "a.jsonl", [row])
        report = self.report()
        self.assertEqual(report["providers"]["claude"]["input"], 125)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("PRIVATE", render(report))


if __name__ == "__main__":
    unittest.main()
