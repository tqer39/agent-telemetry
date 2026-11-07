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
    runs: dict[str, Run] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            run_id = data.get("run_id")
            first_round_passed = data.get("first_round_passed", False)
            if not isinstance(run_id, str) or not run_id or not isinstance(first_round_passed, bool):
                continue
            # 同じ実行 ID が複数あると Prometheus の系列が重複するので、1 件に絞る。
            runs[run_id] = Run(
                run_id=run_id,
                repo=str(data.get("repo", "unknown")),
                kind=str(data.get("kind", "unknown")),
                agent=str(data.get("agent", "unknown")),
                model=str(data.get("model", "unknown")),
                effort=str(data.get("effort", "unknown")),
                status=str(data.get("status", "unknown")),
                repairs=int(data.get("repairs", 0)),
                first_round_passed=first_round_passed,
            )
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            continue
    return list(runs.values())
