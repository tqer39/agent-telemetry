#!/usr/bin/env python3
"""Merge client-config/* into ~/.claude/settings.json and ~/.codex/config.toml.

Desktop apps never read ~/.zshrc, so environment variables must live in these files.
Backups are written next to each file before changing it. Idempotent.

  python3 scripts/apply-client-config.py           # dry run (prints what would change)
  python3 scripts/apply-client-config.py --apply   # write
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE_SRC = REPO / "client-config" / "claude-settings.json"
CODEX_SRC = REPO / "client-config" / "codex-config.toml"


def backup(path: Path) -> None:
    if path.exists():
        dst = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(path, dst)
        print(f"  backup: {dst}")


def claude(apply: bool) -> None:
    target = Path.home() / ".claude" / "settings.json"
    current = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    wanted = json.loads(CLAUDE_SRC.read_text(encoding="utf-8"))["env"]
    env = current.setdefault("env", {})
    changes = {k: v for k, v in wanted.items() if env.get(k) != v}
    print(f"[claude] {target}: {len(changes)} env key(s) to set")
    for k, v in changes.items():
        print(f"  {k} = {v}  (was: {env.get(k, '<unset>')})")
    if apply and changes:
        backup(target)
        env.update(changes)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def codex(apply: bool) -> None:
    target = Path.home() / ".codex" / "config.toml"
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if "otel" in tomllib.loads(text):
        print(f"[codex] {target}: an [otel] section already exists; leaving it untouched.")
        print(f"        Compare it with {CODEX_SRC} by hand.")
        return
    block = CODEX_SRC.read_text(encoding="utf-8")
    merged = text.rstrip("\n") + ("\n\n" if text else "") + block
    tomllib.loads(merged)  # refuse to write invalid TOML
    print(f"[codex] {target}: [otel] section will be appended")
    if apply:
        backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    claude(args.apply)
    codex(args.apply)
    if not args.apply:
        print("\nDry run only. Re-run with --apply, then fully quit and reopen both Desktop apps.")
    else:
        print("\nDone. Fully quit and reopen Claude Desktop and Codex Desktop to pick up the settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
