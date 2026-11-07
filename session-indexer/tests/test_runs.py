"""agent-harness の実行記録の取り込みを確認する。"""

from __future__ import annotations

import json
from pathlib import Path

from session_indexer.claude import parse_claude_file
from session_indexer.codex import parse_codex_file
from session_indexer.indexer import Indexer
from session_indexer.metrics import SessionCollector
from session_indexer.model import SessionSummary, run_id_from_cwd
from session_indexer.runs import load_runs


def test_run_id_from_cwd() -> None:
    assert run_id_from_cwd("/w/app-worktrees/agent-20261109-060102-ab12") == "20261109-060102-ab12"
    assert run_id_from_cwd("/s/runs/x.d/review-20261109-060102-ab12") == "20261109-060102-ab12"
    assert run_id_from_cwd("/w/app") == ""
    assert run_id_from_cwd("/w/agent-telemetry") == ""


def test_load_runs_reads_records_and_skips_broken_files(tmp_path: Path) -> None:
    record = {
        "run_id": "r1",
        "repo": "o/n",
        "kind": "bugfix",
        "agent": "codex",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "status": "completed",
        "repairs": 1,
        "first_round_passed": False,
    }
    (tmp_path / "r1.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    runs = load_runs(tmp_path)
    assert [run.run_id for run in runs] == ["r1"]
    assert runs[0].effort == "high"
    assert runs[0].repairs == 1


def test_load_runs_missing_directory(tmp_path: Path) -> None:
    assert load_runs(tmp_path / "none") == []


def test_parsers_record_run_id(tmp_path: Path) -> None:
    cwd = "/w/app-worktrees/agent-20261109-060102-ab12"
    claude = tmp_path / "c.jsonl"
    row = {
        "sessionId": "s",
        "cwd": cwd,
        "type": "assistant",
        "timestamp": "2026-10-01T00:00:00Z",
        "message": {"id": "m", "model": "claude", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": []},
    }
    claude.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert parse_claude_file(claude, 300).run_id == "20261109-060102-ab12"
    codex = tmp_path / "x.jsonl"
    rows = [
        {
            "timestamp": "2026-10-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "t", "cwd": cwd, "git": {"repository_url": "https://github.com/o/app.git"}},
        },
        {
            "timestamp": "2026-10-01T00:00:10Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 1, "output_tokens": 1}},
            },
        },
    ]
    codex.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    summary = parse_codex_file(codex, 300)
    assert summary.run_id == "20261109-060102-ab12"
    assert summary.project == "app"


def test_collector_exposes_runs(tmp_path: Path, monkeypatch) -> None:
    record = {"run_id": "r1", "repo": "o/n", "effort": "high", "status": "completed", "repairs": 2}
    (tmp_path / "r1.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path))
    families = {family.name: family for family in SessionCollector(Indexer(tmp_path / "c", tmp_path / "x", 14, 300)).collect()}
    info = families["agent_run_info"].samples[0].labels
    assert info["run_id"] == "r1" and info["effort"] == "high"
    assert families["agent_run_repairs"].samples[0].value == 2


def test_load_runs_skips_invalid_flag_and_deduplicates(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps({"run_id": "r1", "first_round_passed": "false"}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"run_id": "r2", "status": "old"}), encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"run_id": "r2", "status": "new"}), encoding="utf-8")
    runs = load_runs(tmp_path)
    assert [(run.run_id, run.status) for run in runs] == [("r2", "new")]


def test_merge_keeps_run_id() -> None:
    first = SessionSummary(tool="codex", session_id="s")
    second = SessionSummary(tool="codex", session_id="s", run_id="20261109-060102-ab12")
    first.merge(second)
    assert first.run_id == "20261109-060102-ab12"
    first.merge(SessionSummary(tool="codex", session_id="s", run_id="other"))
    assert first.run_id == "20261109-060102-ab12"


def test_load_runs_skips_non_object_and_invalid_run_id(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("null", encoding="utf-8")
    (tmp_path / "b.json").write_text("[]", encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"run_id": None}), encoding="utf-8")
    (tmp_path / "d.json").write_text(json.dumps({"run_id": ""}), encoding="utf-8")
    (tmp_path / "e.json").write_text(json.dumps({"run_id": 1}), encoding="utf-8")
    (tmp_path / "f.json").write_text(json.dumps({"run_id": "ok"}), encoding="utf-8")
    assert [run.run_id for run in load_runs(tmp_path)] == ["ok"]


def test_load_runs_skips_out_of_range_number(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"run_id": "bad", "repairs": 1e999}', encoding="utf-8")
    (tmp_path / "b.json").write_text('{"run_id": "ok", "repairs": 1}', encoding="utf-8")
    assert [run.run_id for run in load_runs(tmp_path)] == ["ok"]


def test_load_runs_skips_repairs_that_cannot_be_exported(tmp_path: Path) -> None:
    bad = {"huge": "1" + "0" * 400, "negative": "-1", "text": '"1"', "flag": "true", "fraction": "1.5"}
    for name, value in bad.items():
        (tmp_path / f"{name}.json").write_text(f'{{"run_id": "{name}", "repairs": {value}}}', encoding="utf-8")
    (tmp_path / "ok.json").write_text('{"run_id": "ok", "repairs": 2}', encoding="utf-8")
    assert [(run.run_id, run.repairs) for run in load_runs(tmp_path)] == [("ok", 2)]
