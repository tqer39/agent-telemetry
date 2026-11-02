import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_telemetry.__main__ import main
from agent_telemetry.analysis import analyze, render_text


def session(sid, total=200_000, codes=("input_growth",), **extra):
    return dict(
        {
            "session_id": sid,
            "provider": "codex",
            "repository": "example/repo",
            "anchor": "session-" + sid,
            "total": total,
            "input": total - 100,
            "output": 100,
            "requests": 10,
            "cache_read_rate": 0.98,
            "hints": [{"code": c, "title": c, "evidence": "measured evidence"} for c in codes],
            "hotspots": [{"source": "/logs/s.jsonl.zst", "line": 42, "timestamp": "2026-10-31"}],
        },
        **extra,
    )


def snapshot(*sessions):
    return {
        "generated_at": "2026-10-31T00:00:00Z",
        "start_date": None,
        "end_date": "2026-10-31",
        "timezone": "Asia/Tokyo",
        "sessions": list(sessions),
        "diagnostics": {},
    }


class AnalysisTests(unittest.TestCase):
    def test_priority_combines_consumption_and_signals_not_total_alone(self):
        r = analyze(snapshot(session("largest", 500_000), session("joint", 200_000, ("input_growth", "large_tool_output"))), top=1)
        self.assertEqual(r["recommendations"][0]["session_id"], "joint")
        item = r["recommendations"][0]
        self.assertEqual(item["priority_weight"], 4)
        self.assertEqual(item["first_action"]["code"], "large_tool_output")
        self.assertAlmostEqual(r["selected_share"], 200_000 / 700_000)
        self.assertIn("因果関係は未確認", item["selection_reason"])

    def test_wait_heavy_repetition_is_discounted_and_output_is_first(self):
        s = session(
            "s",
            codes=("repeated_tools", "large_tool_output"),
            repeated_tool_calls=100,
            repeated_tool_groups=[{"tool": "clock.sleep", "calls": 81}],
        )
        item = analyze(snapshot(s))["recommendations"][0]
        self.assertTrue(item["wait_heavy"])
        self.assertEqual(item["known_wait_repeats"], 80)
        self.assertEqual(item["priority_weight"], 3)
        self.assertEqual(item["first_action"]["code"], "large_tool_output")
        self.assertIn("80", next(h["evidence"] for h in item["causes"] if h["code"] == "repeated_tools"))

    def test_wait_only_action_checks_model_polls_not_sleep_duration(self):
        s = session("s", codes=("repeated_tools",), repeated_tool_calls=3, repeated_tool_groups=[{"tool": "clock.sleep", "calls": 4}])
        action = analyze(snapshot(s))["recommendations"][0]["first_action"]
        self.assertIn("モデルの再呼出", action["steps"])

    def test_repeated_errors_are_checked_before_retry(self):
        s = session("s", codes=("repeated_tools", "tool_errors"), tool_errors=3)
        item = analyze(snapshot(s))["recommendations"][0]
        self.assertEqual(item["priority_weight"], 6)
        self.assertEqual(item["first_action"]["code"], "tool_errors")
        s["tool_errors"] = 1
        self.assertEqual(analyze(snapshot(s))["recommendations"][0]["first_action"]["code"], "repeated_tools")

    def test_unknown_usage_no_hints_unknown_rules_and_small_sessions_are_not_selected(self):
        r = analyze(
            snapshot(
                session("empty", requests=0),
                session("none", codes=()),
                session("future", codes=("unknown_rule",)),
                session("small", total=99_999),
                session("boundary", total=100_000),
            )
        )
        self.assertEqual(r["candidate_sessions"], 1)
        self.assertEqual(r["usage_missing_sessions"], 1)
        self.assertEqual(r["recommendations"][0]["session_id"], "boundary")

    def test_reproducible_ties_and_input_is_not_modified(self):
        data = snapshot(session("b"), session("a"))
        original = copy.deepcopy(data)
        first = analyze(data)
        self.assertEqual(first, analyze(data))
        self.assertEqual(data, original)
        data["sessions"].reverse()
        self.assertEqual(first, analyze(data))
        self.assertEqual([r["session_id"] for r in first["recommendations"]], ["a", "b"])

    def test_reference_and_verification_are_in_text_without_raw_content(self):
        text = render_text(analyze(snapshot(session("s"))))
        self.assertIn("/logs/s.jsonl.zst:42", text)
        self.assertIn("最初に試す", text)
        self.assertIn("検証:", text)
        self.assertIn("98.0%", text)
        self.assertIn("削減可能量ではない", text)

    def test_empty_candidates_are_not_declared_efficient_and_diagnostics_are_preserved(self):
        data = snapshot(session("small", total=100))
        data["diagnostics"] = {"corrupt_compressed_files": 2}
        text = render_text(analyze(data))
        self.assertIn("効率や品質が良いという判定ではありません", text)
        self.assertIn("corrupt_compressed_files", text)

    def test_bad_snapshots_and_parameters_are_rejected(self):
        for data in (
            {},
            {"sessions": None},
            {"sessions": [None]},
            snapshot(session("s", input="bad")),
            snapshot(session("s", tool_evidence="bad")),
            snapshot(session("s", cache_read_rate=2)),
        ):
            with self.assertRaises(ValueError):
                analyze(data)
        with self.assertRaises(ValueError):
            analyze(snapshot(), top=0)

    def test_cli_snapshot_analysis_never_scans_logs_and_can_emit_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.json"
            source.write_text(json.dumps(snapshot(session("s"))))
            args = ["agent_telemetry", "--analyze", "--from-report", str(source), "--json", "--top", "1"]
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", args),
                patch("agent_telemetry.__main__.scan", side_effect=AssertionError("must not read logs")),
                contextlib.redirect_stdout(stdout),
            ):
                main()
            self.assertEqual(json.loads(stdout.getvalue())["method"], "rules-v1")
            original = source.read_bytes()
            with (
                patch.object(sys, "argv", args + ["--output", str(source)]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main()
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
