"""Tool-agnostic per-session summary shared by the Claude and Codex parsers."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath

_WS = re.compile(r"\s+")
_SHELLS = {"bash", "sh", "zsh"}


def normalize_command(command: object) -> str | None:
    """Turn a shell command (string or argv list) into a comparable string.

    `["bash", "-lc", "pytest -q"]` and `"pytest  -q"` both become `"pytest -q"`.
    """
    if command is None:
        return None
    if isinstance(command, list):
        parts = [str(p) for p in command]
        wrapped = len(parts) >= 3 and PurePath(parts[0]).name in _SHELLS and parts[1] in {"-c", "-lc"}
        text = " ".join(parts[2:]) if wrapped else shlex.join(parts)
    else:
        text = str(command)
    text = _WS.sub(" ", text).strip()
    return text or None


def project_from_cwd(cwd: str) -> str:
    """Repository name for a cwd; Claude worktrees live in <repo>/.claude/worktrees/<name>."""
    parts = PurePath(cwd).parts
    if ".claude" in parts and parts.index(".claude") > 0:
        parts = parts[: parts.index(".claude")]
    return parts[-1] if len(parts) > 1 else "unknown"


@dataclass
class SessionSummary:
    tool: str
    session_id: str
    project: str = "unknown"
    models: set[str] = field(default_factory=set)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    active_seconds: float = 0.0

    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0  # subset of output for Codex; reported separately

    tool_calls: int = 0
    tool_failures: int = 0
    shell_commands: int = 0
    repeated_commands: int = 0
    retries_after_failure: int = 0
    parse_errors: int = 0

    # working state (not exported)
    _seen_commands: set[str] = field(default_factory=set, repr=False)
    _failed_commands: set[str] = field(default_factory=set, repr=False)
    _timestamps: list[datetime] = field(default_factory=list, repr=False)

    # ---- recording helpers -------------------------------------------------
    def observe_ts(self, ts: datetime | None) -> None:
        if ts is None:
            return
        self._timestamps.append(ts)
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts

    def record_shell_command(self, command: str | None) -> None:
        if not command:
            return
        self.shell_commands += 1
        if command in self._seen_commands:
            self.repeated_commands += 1
        if command in self._failed_commands:
            self.retries_after_failure += 1
        self._seen_commands.add(command)

    def record_result(self, command: str | None, failed: bool) -> None:
        if failed:
            self.tool_failures += 1
            if command:
                self._failed_commands.add(command)
        elif command:
            # a later success clears the failure, so re-runs after a fix aren't counted twice
            self._failed_commands.discard(command)

    def finalize(self, idle_gap_seconds: float) -> SessionSummary:
        """Compute active time: sum of gaps between events, ignoring idle gaps."""
        stamps = sorted(self._timestamps)
        active = 0.0
        for prev, cur in zip(stamps, stamps[1:], strict=False):
            gap = (cur - prev).total_seconds()
            if 0 < gap <= idle_gap_seconds:
                active += gap
        self.active_seconds = active
        return self

    # ---- derived values ----------------------------------------------------
    @property
    def wall_seconds(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return (self.last_ts - self.first_ts).total_seconds()

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_ratio(self) -> float:
        total = self.total_input_tokens
        return self.cache_read_tokens / total if total else 0.0

    def merge(self, other: SessionSummary) -> None:
        """Merge another file of the same session (e.g. Claude subagent transcripts)."""
        if self.project == "unknown":
            self.project = other.project
        self.models |= other.models
        for ts in other._timestamps:
            self.observe_ts(ts)
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "tool_calls",
            "tool_failures",
            "shell_commands",
            "repeated_commands",
            "retries_after_failure",
            "parse_errors",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Mixing naive and aware values raises TypeError on comparison; treat naive as UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
