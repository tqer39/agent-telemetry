"""Scan transcript directories, parse changed files, merge per session."""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .claude import parse_claude_file
from .codex import parse_codex_file
from .model import SessionSummary

log = logging.getLogger(__name__)
Parser = Callable[[Path, float], SessionSummary | None]


@dataclass
class Source:
    tool: str
    root: Path
    parser: Parser


@dataclass
class ScanStats:
    files_scanned: int = 0
    files_parsed: int = 0
    file_errors: int = 0
    last_scan_ts: float = 0.0
    sources_missing: list[str] = field(default_factory=list)


class Indexer:
    def __init__(
        self,
        claude_root: Path | None,
        codex_root: Path | None,
        lookback_days: float,
        idle_gap_seconds: float,
    ) -> None:
        self.sources: list[Source] = []
        if claude_root is not None:
            self.sources.append(Source("claude-code", claude_root, parse_claude_file))
        if codex_root is not None:
            self.sources.append(Source("codex", codex_root, parse_codex_file))
        self.lookback_seconds = lookback_days * 86400
        self.idle_gap_seconds = idle_gap_seconds
        self._cache: dict[Path, tuple[float, int, SessionSummary | None]] = {}
        self.sessions: list[SessionSummary] = []
        self.stats = ScanStats()

    def scan(self) -> list[SessionSummary]:
        cutoff = time.time() - self.lookback_seconds
        stats = ScanStats(last_scan_ts=time.time())
        live: set[Path] = set()

        for source in self.sources:
            if not source.root.is_dir():
                stats.sources_missing.append(source.tool)
                continue
            for path in source.root.rglob("*.jsonl"):
                try:
                    st = path.stat()
                except OSError:
                    stats.file_errors += 1
                    continue
                if st.st_mtime < cutoff:
                    continue
                live.add(path)
                stats.files_scanned += 1
                cached = self._cache.get(path)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    continue
                try:
                    summary = source.parser(path, self.idle_gap_seconds)
                except Exception as exc:  # one bad transcript must not abort the whole scan
                    log.warning("failed to parse transcript (%s)", type(exc).__name__)
                    self._cache.pop(path, None)
                    stats.file_errors += 1
                    continue
                stats.files_parsed += 1
                self._cache[path] = (st.st_mtime, st.st_size, summary)

        for stale in set(self._cache) - live:
            del self._cache[stale]

        self.sessions = self._merge()
        self.stats = stats
        return self.sessions

    def _merge(self) -> list[SessionSummary]:
        merged: dict[tuple[str, str], SessionSummary] = {}
        for _, _, summary in self._cache.values():
            if summary is None:
                continue
            key = (summary.tool, summary.session_id)
            if key in merged:
                merged[key].merge(summary)
            else:
                merged[key] = copy.deepcopy(summary)  # never mutate cached parse results
        for s in merged.values():
            s.finalize(self.idle_gap_seconds)
        return sorted(merged.values(), key=lambda s: s.first_ts.timestamp() if s.first_ts else 0)
