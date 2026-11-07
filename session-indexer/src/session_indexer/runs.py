"""agent-harness の実行記録を読み取る。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Run:
    run_id: str
    repo: str
    kind: str
    agent: str
    model: str
    effort: str
    status: str
    repairs: int
    first_round_passed: bool


def load_runs(directory: Path) -> list[Run]:
    """読めない記録は飛ばす。書き込み途中の記録で計測を止めないため。"""
    if not directory.is_dir():
        return []
    runs: list[Run] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            runs.append(
                Run(
                    run_id=str(data["run_id"]),
                    repo=str(data.get("repo", "unknown")),
                    kind=str(data.get("kind", "unknown")),
                    agent=str(data.get("agent", "unknown")),
                    model=str(data.get("model", "unknown")),
                    effort=str(data.get("effort", "unknown")),
                    status=str(data.get("status", "unknown")),
                    repairs=int(data.get("repairs", 0)),
                    first_round_passed=bool(data.get("first_round_passed", False)),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return runs
