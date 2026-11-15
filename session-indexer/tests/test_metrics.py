"""セッションと indexer の計測が、追加の状態ディレクトリなしで収集できることを確認する。"""

from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from session_indexer.indexer import Indexer
from session_indexer.metrics import SessionCollector
from session_indexer.model import SessionSummary


def test_metrics_from_transcripts_only(tmp_path: Path) -> None:
    indexer = Indexer(tmp_path / "claude", tmp_path / "codex", 14, 300)
    indexer.scan()
    collector = SessionCollector(indexer)
    families = {family.name for family in collector.collect()}
    assert families == {
        "agent_session_info",
        "agent_session_start_time_seconds",
        "agent_session_wall_seconds",
        "agent_session_active_seconds",
        "agent_session_tokens",
        "agent_session_cache_hit_ratio",
        "agent_session_tool_calls",
        "agent_session_tool_failures",
        "agent_session_shell_commands",
        "agent_session_repeated_commands",
        "agent_session_retries_after_failure",
        "agent_session_parse_errors",
        "agent_indexer_files_scanned",
        "agent_indexer_file_errors",
        "agent_indexer_last_scan_timestamp_seconds",
        "agent_indexer_source_missing",
    }
    registry = CollectorRegistry()
    registry.register(collector)
    exposition = generate_latest(registry).decode()
    assert 'agent_indexer_source_missing{tool="claude-code"} 1.0' in exposition
    assert 'agent_indexer_source_missing{tool="codex"} 1.0' in exposition


def test_metrics_keep_session_values_without_extra_state(tmp_path: Path) -> None:
    indexer = Indexer(tmp_path / "claude", tmp_path / "codex", 14, 300)
    session = SessionSummary(tool="codex", session_id="session", project="project")
    session.models.add("model")
    session.observe_ts(datetime(2026, 11, 15, tzinfo=UTC))
    session.observe_ts(datetime(2026, 11, 15, 0, 0, 10, tzinfo=UTC))
    session.input_tokens = 3
    session.output_tokens = 2
    session.tool_failures = 1
    session.finalize(300)
    indexer.sessions = [session]
    registry = CollectorRegistry()
    registry.register(SessionCollector(indexer))
    text = generate_latest(registry).decode()
    assert 'agent_session_wall_seconds{project="project",session_id="session",tool="codex"} 10.0' in text
    assert 'agent_session_tokens{project="project",session_id="session",tool="codex",type="input"} 3.0' in text
    assert 'agent_session_tool_failures{project="project",session_id="session",tool="codex"} 1.0' in text
