"""Expose the latest scan as Prometheus gauges (rebuilt on every scrape)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from .indexer import Indexer
from .runs import load_runs

_LABELS = ["tool", "session_id", "project"]


class SessionCollector(Collector):
    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer

    def collect(self) -> Iterator[GaugeMetricFamily]:
        sessions = list(self.indexer.sessions)

        def gauge(name: str, doc: str, extra: list[str] | None = None) -> GaugeMetricFamily:
            return GaugeMetricFamily(name, doc, labels=_LABELS + (extra or []))

        info = gauge("agent_session_info", "Session metadata (value is always 1)", ["model"])
        start = gauge("agent_session_start_time_seconds", "Unix time of the first event")
        wall = gauge("agent_session_wall_seconds", "Last event minus first event")
        active = gauge("agent_session_active_seconds", "Sum of event gaps shorter than IDLE_GAP_SECONDS")
        tokens = gauge("agent_session_tokens", "Tokens by type", ["type"])
        cache = gauge("agent_session_cache_hit_ratio", "cache_read / (input + cache_read + cache_creation)")
        calls = gauge("agent_session_tool_calls", "Tool invocations")
        fails = gauge("agent_session_tool_failures", "Tool results flagged as errors / non-zero exit")
        shell = gauge("agent_session_shell_commands", "Shell commands executed")
        repeated = gauge("agent_session_repeated_commands", "Shell commands identical to an earlier one")
        retries = gauge("agent_session_retries_after_failure", "Re-runs of a command whose last run failed")
        errors = gauge("agent_session_parse_errors", "Unparseable lines in the transcript")

        for s in sessions:
            base = [s.tool, s.session_id, s.project]
            info.add_metric(base + [",".join(sorted(s.models)) or "unknown"], 1)
            if s.first_ts:
                start.add_metric(base, s.first_ts.timestamp())
            wall.add_metric(base, s.wall_seconds)
            active.add_metric(base, s.active_seconds)
            for ttype, value in (
                ("input", s.input_tokens),
                ("output", s.output_tokens),
                ("cache_read", s.cache_read_tokens),
                ("cache_creation", s.cache_creation_tokens),
                ("reasoning", s.reasoning_tokens),
            ):
                tokens.add_metric(base + [ttype], value)
            cache.add_metric(base, s.cache_hit_ratio)
            calls.add_metric(base, s.tool_calls)
            fails.add_metric(base, s.tool_failures)
            shell.add_metric(base, s.shell_commands)
            repeated.add_metric(base, s.repeated_commands)
            retries.add_metric(base, s.retries_after_failure)
            errors.add_metric(base, s.parse_errors)

        yield from (info, start, wall, active, tokens, cache, calls, fails, shell, repeated, retries, errors)

        session_run = GaugeMetricFamily(
            "agent_session_run",
            "agent-harness run for a session (value is always 1)",
            labels=["tool", "session_id", "run_id"],
        )
        for s in sessions:
            if s.run_id:
                session_run.add_metric([s.tool, s.session_id, s.run_id], 1)
        run_labels = ["run_id", "repo", "kind", "agent", "model", "effort", "status"]
        run_info = GaugeMetricFamily("agent_run_info", "agent-harness run (value is always 1)", labels=run_labels)
        run_repairs = GaugeMetricFamily("agent_run_repairs", "Repair rounds in the run", labels=["run_id"])
        run_first = GaugeMetricFamily("agent_run_first_round_passed", "1 when the run passed without repairs", labels=["run_id"])
        for run in load_runs(Path(os.environ.get("AGENT_RUNS_DIR", "/data/agent-runs"))):
            run_info.add_metric([run.run_id, run.repo, run.kind, run.agent, run.model, run.effort, run.status], 1)
            run_repairs.add_metric([run.run_id], run.repairs)
            run_first.add_metric([run.run_id], 1 if run.first_round_passed else 0)
        yield from (session_run, run_info, run_repairs, run_first)

        st = self.indexer.stats
        scanned = GaugeMetricFamily("agent_indexer_files_scanned", "Transcript files inside the lookback window")
        scanned.add_metric([], st.files_scanned)
        ferr = GaugeMetricFamily("agent_indexer_file_errors", "Files that could not be read in the last scan")
        ferr.add_metric([], st.file_errors)
        last = GaugeMetricFamily("agent_indexer_last_scan_timestamp_seconds", "Unix time of the last scan")
        last.add_metric([], st.last_scan_ts)
        missing = GaugeMetricFamily("agent_indexer_source_missing", "1 when a transcript dir is not mounted", labels=["tool"])
        for src in self.indexer.sources:
            missing.add_metric([src.tool], 1 if src.tool in st.sources_missing else 0)
        yield from (scanned, ferr, last, missing)
