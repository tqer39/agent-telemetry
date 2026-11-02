import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from agent_telemetry.efficiency import error_status, remote_name, repository
from agent_telemetry.report import render
from agent_telemetry.usage import scan


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        repository.cache_clear()

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def report(self, days=30):
        return scan(self.claude, self.codex, days, UTC, datetime(2026, 10, 31, 12, tzinfo=UTC))

    def row(self, index, input_tokens=100, cached=80, sid="s1"):
        usage = {"input_tokens": input_tokens, "output_tokens": 20}
        if cached is not None:
            usage["cache_read_input_tokens"] = cached
        timestamp = (datetime(2026, 10, 31, tzinfo=UTC) + timedelta(seconds=index)).isoformat()
        return {
            "type": "assistant",
            "sessionId": sid,
            "timestamp": timestamp,
            "message": {"id": f"{sid}-{index}", "model": "test", "usage": usage, "content": [{"type": "text", "text": "PRIVATE_PROMPT"}]},
        }

    def test_growth_cache_and_hints_have_measured_evidence(self):
        rows = [self.row(i, n - 100, 100) for i, n in enumerate([10_000] * 3 + [50_000] * 3 + [100_000] * 3)]
        self.write(self.claude / "s.jsonl", rows)
        result = self.report()
        s = result["sessions"][0]
        self.assertEqual(s["input_growth"], 10)
        self.assertAlmostEqual(s["cache_read_rate"], 900 / 480_000)
        self.assertEqual(s["non_cached_input"], 479_100)
        self.assertEqual({h["code"] for h in s["hints"]}, {"low_cache", "input_growth"})
        self.assertTrue(all(h["evidence"] and h["suggestion"] for h in s["hints"]))
        self.assertEqual(s["hotspots"][0]["line"], 7)
        self.assertEqual(s["input_timeline"][-1]["record"], 9)
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(result))
        self.assertNotIn("PRIVATE_PROMPT", render(result))

    def test_missing_cache_and_usage_are_unknown_not_efficient(self):
        self.write(self.claude / "s.jsonl", [self.row(0, 200_000, None)])
        self.write(
            self.codex / "sessions/empty.jsonl", [{"type": "session_meta", "timestamp": "2026-10-31T00:00:00Z", "payload": {"id": "empty"}}]
        )
        s, empty = self.report()["sessions"]
        self.assertIsNone(s["cache_read_rate"])
        self.assertIsNone(s["non_cached_input"])
        self.assertEqual(s["hints"], [])
        self.assertIn("未判定", s["status"])
        self.assertIn("判断不可", empty["status"])
        self.assertIsNone(empty["input_per_record"])

    def test_repeated_tools_errors_and_large_output_without_exporting_arguments(self):
        rows = []
        for i in range(4):
            call = {"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {"command": "SECRET_COMMAND"}}
            row = self.row(i)
            row["message"]["content"] = [call, call]  # replayed block must not count twice
            rows.extend(
                [
                    row,
                    {
                        "type": "user",
                        "sessionId": "s1",
                        "timestamp": row["timestamp"],
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"c{i}",
                                    "is_error": i == 0,
                                    "content": "PRIVATE_TOOL_RESULT" + "x" * 25_000,
                                }
                            ]
                        },
                    },
                ]
            )
        self.write(self.claude / "s.jsonl", rows)
        self.write(self.claude / "copy.jsonl", rows)
        result = self.report()
        s = result["sessions"][0]
        self.assertEqual((s["tool_calls"], s["repeated_tool_calls"], s["tool_errors"], s["known_result_status"]), (4, 3, 1, 4))
        self.assertEqual({h["code"] for h in s["hints"]}, {"repeated_tools", "tool_errors", "large_tool_output"})
        self.assertEqual(s["repeated_tool_groups"][0]["calls"], 4)
        self.assertEqual((s["repeated_tool_groups"][0]["first_line"], s["repeated_tool_groups"][0]["last_line"]), (1, 7))
        self.assertIn("Bash", s["hotspots"][-1]["preceding_tools"])
        encoded = json.dumps(result) + render(result)
        for secret in ("SECRET_COMMAND", "PRIVATE_TOOL_RESULT", "fingerprint"):
            self.assertNotIn(secret, encoded)

    def test_period_applies_to_tool_signals_as_well_as_usage(self):
        row = self.row(0)
        old = self.row(1)
        old["timestamp"] = "2026-08-01T00:00:00Z"
        old["message"]["content"] = [{"type": "tool_use", "id": "old", "name": "Bash", "input": {"command": "private"}}]
        self.write(self.claude / "s.jsonl", [old, row])
        s = self.report(days=1)["sessions"][0]
        self.assertEqual((s["requests"], s["tool_calls"]), (1, 0))

    def test_codex_structured_and_textual_status_and_compaction(self):
        rows = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "codex",
                    "cwd": "/deleted/worktree",
                    "git": {"repository_url": "https://user:SECRET_TOKEN@example.com/org/repo.git?secret=HIDDEN"},
                },
            }
        ]
        for i in range(4):
            rows.append(
                {
                    "type": "response_item",
                    "timestamp": "2026-10-31T00:00:00Z",
                    "payload": {"type": "function_call", "name": "exec", "call_id": str(i), "arguments": '{"cmd":"SECRET_COMMAND"}'},
                }
            )
            rows.append(
                {
                    "type": "response_item",
                    "timestamp": "2026-10-31T00:00:00Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": str(i),
                        "output": "Process exited with code 1" if i == 0 else '{"exit_code":0}',
                    },
                }
            )
        rows.extend(
            [
                {"type": "compacted", "timestamp": "2026-10-31T00:00:00Z", "payload": {}},
                {
                    "type": "token_usage_record",
                    "timestamp": "2026-10-31T00:00:01Z",
                    "payload": {"response_id": "r", "usage": {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 10}},
                },
            ]
        )
        self.write(self.codex / "sessions/a.jsonl", rows)
        s = self.report()["sessions"][0]
        self.assertEqual(s["repository"], "example.com/org/repo")
        self.assertEqual((s["tool_calls"], s["tool_errors"], s["compactions"]), (4, 1, 1))
        self.assertEqual(s["hotspots"][0]["preceding_tools"], ["exec"] * 3)
        for secret in ("SECRET_TOKEN", "HIDDEN", "SECRET_COMMAND"):
            self.assertNotIn(secret, json.dumps(self.report()))

    def test_status_detection_does_not_treat_error_word_as_failure(self):
        self.assertIsNone(error_status("0 errors found"))
        self.assertIsNone(error_status("documentation says: Process exited with code 1"))
        self.assertIsNone(error_status({"status": "unknown"}))
        self.assertTrue(error_status('{"metadata":{"exit_code":2}}'))
        self.assertFalse(error_status("Exit code: 0"))
        self.assertTrue(error_status({"is_error": True}))

    def test_timeline_sampling_keeps_endpoints_and_hotspots_keep_unsampled_peak(self):
        rows = [self.row(i, 500_000 if i == 55 else 100) for i in range(120)]
        self.write(self.claude / "s.jsonl", rows)
        s = self.report()["sessions"][0]
        self.assertLessEqual(len(s["input_timeline"]), 60)
        self.assertEqual((s["input_timeline"][0]["record"], s["input_timeline"][-1]["record"]), (1, 120))
        self.assertEqual(s["hotspots"][0]["line"], 56)
        self.assertGreater(s["hotspots"][0]["input"], max(p["input"] for p in s["input_timeline"]))

    def test_invalid_cache_ratio_is_unknown(self):
        rows = [
            {
                "type": "token_usage_record",
                "timestamp": "2026-10-31T00:00:00Z",
                "payload": {"response_id": "r", "usage": {"input_tokens": 100, "cached_input_tokens": 500}},
            }
        ]
        self.write(self.codex / "sessions/s.jsonl", rows)
        self.assertIsNone(self.report()["sessions"][0]["cache_read_rate"])

    def test_usage_trace_keeps_complete_snapshot_when_stale_copy_follows(self):
        complete = self.row(0)
        complete["message"]["usage"]["output_tokens"] = 100
        stale = self.row(0)
        self.write(self.claude / "a-complete.jsonl", [complete])
        self.write(self.claude / "z-stale.jsonl", [stale])
        hotspot = self.report()["sessions"][0]["hotspots"][0]
        self.assertEqual(hotspot["output"], 100)
        self.assertTrue(hotspot["source"].endswith("a-complete.jsonl"))

    def test_large_successful_output_is_retained_even_with_many_errors(self):
        rows = [self.row(0)]
        for i in range(12):
            rows.append(
                {
                    "type": "user",
                    "sessionId": "s1",
                    "timestamp": "2026-10-31T00:00:00Z",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": str(i), "is_error": True, "content": "failed"}]},
                }
            )
        rows.append(
            {
                "type": "user",
                "sessionId": "s1",
                "timestamp": "2026-10-31T00:00:00Z",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "large", "is_error": False, "content": "x" * 30_000}]},
            }
        )
        self.write(self.claude / "s.jsonl", rows)
        s = self.report()["sessions"][0]
        self.assertTrue(any(r["output_chars"] == 30_000 for r in s["tool_evidence"]))
        self.assertLessEqual(len(s["tool_evidence"]), 20)

    def test_remote_normalization_and_deleted_repository(self):
        self.assertEqual(remote_name("git@example.com:org/repo.git"), "example.com/org/repo")
        self.assertEqual(remote_name("ssh://git@example.com/org/repo.git"), "example.com/org/repo")
        self.assertIsNone(remote_name("not a repository"))
        self.assertEqual(repository("/does/not/exist")["basis"], "作業ディレクトリ（Git未確認）")

    @unittest.skipUnless(shutil.which("git"), "Git is unavailable")
    def test_linked_worktrees_group_together_and_ignore_inherited_git_environment(self):
        repo = self.root / "repo"
        repo.mkdir()
        hooks = self.root / "hooks"
        hooks.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def git(*args):
            subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
                [shutil.which("git"), "-C", str(repo), "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false", *args],
                env=env,
                capture_output=True,
                check=True,
            )

        git("init")
        git("-c", "user.name=Telemetry Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        worktree = self.root / "worktree"
        git("worktree", "add", "--detach", str(worktree))
        with patch.dict(os.environ, {"GIT_DIR": "/invalid", "GIT_WORK_TREE": "/invalid"}):
            self.assertEqual(repository(str(repo))["key"], repository(str(worktree))["key"])

    def test_session_repository_and_provider_totals_agree(self):
        self.write(self.claude / "a.jsonl", [self.row(0)])
        self.write(self.claude / "b.jsonl", [self.row(1, 300, 200, sid="s2")])
        r = self.report()
        self.assertEqual([s["session_id"] for s in r["sessions"]], ["s2", "s1"])
        for field in ("input", "output", "requests"):
            self.assertEqual(sum(s[field] for s in r["sessions"]), sum(p[field] for p in r["providers"].values()))
            self.assertEqual(sum(s[field] for s in r["sessions"]), sum(p[field] for p in r["repositories"]))

    def test_html_escapes_trace_metadata_and_all_anchor_links_resolve(self):
        row = self.row(0, sid="session<script>PRIVATE</script>")
        row["cwd"] = '/missing/<img src=x onerror="bad">'
        row["message"]["model"] = "<script>bad</script>"
        self.write(self.claude / "a.jsonl", [row])
        html = render(self.report())
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)

        class Inspect(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []
                self.links = []

            def handle_starttag(self, tag, attrs):
                for k, v in attrs:
                    if k == "id":
                        self.ids.append(v)
                    if k in ("href", "src"):
                        self.links.append(v)

        p = Inspect()
        p.feed(html)
        self.assertEqual(len(p.ids), len(set(p.ids)))
        self.assertTrue(p.links)
        self.assertTrue(all(link.startswith("#") and link[1:] in p.ids for link in p.links))


if __name__ == "__main__":
    unittest.main()
