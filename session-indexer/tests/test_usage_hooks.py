import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agent_telemetry.hooks as hook_module
from agent_telemetry.hooks import MAX_INPUT_BYTES, configuration, handle, tip_for


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"

    def payload(self, **extra):
        return dict(
            {
                "session_id": "private-session",
                "hook_event_name": "PostToolUse",
                "tool_input": {"command": "SECRET COMMAND"},
                "tool_response": "PRIVATE OUTPUT " * 2000,
            },
            **extra,
        )

    def test_large_output_is_advice_only_no_output_replacement_or_context_injection(self):
        category, out = tip_for(self.payload())
        self.assertEqual(category, "large_output")
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("トークン数ではありません", out["systemMessage"])
        self.assertNotIn("PRIVATE OUTPUT", json.dumps(out))
        self.assertNotIn("SECRET COMMAND", json.dumps(out))

    def test_only_result_size_triggers_and_boundary_is_inclusive(self):
        self.assertIsNone(tip_for(self.payload(tool_input="x" * 100_000, tool_response="small")))
        self.assertIsNone(tip_for(self.payload(tool_response="x" * 19999)))
        self.assertIsNotNone(tip_for(self.payload(tool_response="x" * 20000)))
        self.assertIsNotNone(tip_for(self.payload(tool_response={"stdout": "x" * 20000})))

    def test_compaction_source_only_and_short_fixed_reminder(self):
        p = self.payload(hook_event_name="SessionStart", source="compact")
        _, out = tip_for(p)
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("検証結果", ctx["additionalContext"])
        self.assertLess(len(ctx["additionalContext"]), 250)
        for source in ("startup", "resume", "clear", "fork"):
            self.assertIsNone(tip_for(dict(p, source=source)))
        self.assertIsNone(tip_for(self.payload(hook_event_name="PreCompact")))

    def test_cooldown_survives_calls_and_is_scoped_by_session_and_category(self):
        p = self.payload()
        self.assertIsNotNone(handle(p, self.state, now=1000))
        self.assertIsNone(handle(p, self.state, now=1899))
        self.assertIsNotNone(handle(p, self.state, now=1900))
        self.assertIsNotNone(handle(dict(p, session_id="other"), self.state, now=1900))
        self.assertIsNotNone(handle(dict(p, hook_event_name="SessionStart", source="compact"), self.state, now=1900))
        self.assertIsNone(handle(dict(p, hook_event_name="SessionStart", source="compact"), self.state, now=1901))

    def test_concurrent_hooks_emit_once(self):
        handle(self.payload(session_id="initialize"), self.state, now=1000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(lambda _: handle(self.payload(), self.state, now=1000), range(16)))
        self.assertEqual(sum(x is not None for x in out), 1)

    def test_cache_contains_only_hash_and_time_and_does_not_store_transcript(self):
        handle(self.payload(transcript_path="/must/not/read"), self.state, now=1000)
        with sqlite3.connect(self.state / "notifications.sqlite3") as con:
            [(key, seen)] = con.execute("SELECT key, seen FROM notifications").fetchall()
        self.assertEqual((len(key), seen), (64, 1000))
        for path in self.state.iterdir():
            raw = path.read_bytes()
            for secret in (b"SECRET COMMAND", b"PRIVATE OUTPUT", b"private-session", b"/must/not/read"):
                self.assertNotIn(secret, raw)

    def test_missing_session_unknown_events_and_storage_failure_are_silent(self):
        for p in (None, {}, [], self.payload(session_id=None), self.payload(hook_event_name="Stop")):
            self.assertIsNone(handle(p, self.state))
        self.assertFalse(self.state.exists())
        self.state.write_text("not a directory")
        self.assertIsNone(handle(self.payload(), self.state))

    def test_generated_command_works_from_another_directory(self):
        config = configuration()
        handler = config["hooks"]["SessionStart"][0]
        self.assertEqual(handler["matcher"], "compact")
        command = shlex.split(handler["hooks"][0]["command"])
        self.assertTrue(Path(command[1]).is_absolute())
        p = self.payload(hook_event_name="SessionStart", source="compact")
        result = subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
            [*command, "--state-dir", str(self.state)], input=json.dumps(p), text=True, cwd=self.temp.name, capture_output=True, check=True
        )
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertFalse(result.stderr)

    def test_invalid_or_oversized_stdin_never_blocks(self):
        script = Path(hook_module.__file__)
        for raw in (b"bad json", b"[]", b"", b"x" * (MAX_INPUT_BYTES + 1)):
            p = subprocess.run(  # noqa: S603 -- fixed synthetic test commands without a shell
                [sys.executable, str(script), "--state-dir", str(self.state)], input=raw, capture_output=True, check=True
            )
            self.assertEqual((p.stdout, p.stderr), (b"", b""))


if __name__ == "__main__":
    unittest.main()
