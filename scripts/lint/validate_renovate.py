"""Run Renovate with mise's Node and npx, regardless of the caller's PATH."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

RENOVATE_VERSION = "44.155.8"


def main(args: list[str] | None = None) -> int:
    mise = shutil.which("mise")
    if mise is None:
        print("mise が必要です。mise install を実行してください。", file=sys.stderr)
        return 1
    try:
        node, npx = (
            Path(
                subprocess.check_output([mise, "which", tool], text=True).strip()
            ).resolve()
            for tool in ("node", "npx")
        )
        # Windows の npx.cmd は Node で直接実行できないため、同梱の JS を使う。
        if npx.suffix == ".cmd":
            npx = npx.parent / "node_modules/npm/bin/npx-cli.js"
        if not node.is_file() or not npx.is_file():
            raise FileNotFoundError(
                "固定した Node/npx がありません。mise install を実行してください。"
            )
        env = os.environ.copy()
        env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            [
                str(node),
                str(npx),
                "--yes",
                "--prefer-offline",
                f"--package=renovate@{RENOVATE_VERSION}",
                "--",
                "renovate-config-validator",
                *(sys.argv[1:] if args is None else args),
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if "RE2 not usable" in result.stdout + result.stderr:
            print(
                "RE2 の初期化に失敗したため、Renovate 検査を失敗として扱います。",
                file=sys.stderr,
            )
            return 1
        return result.returncode
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Renovate 検査を起動できません: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
