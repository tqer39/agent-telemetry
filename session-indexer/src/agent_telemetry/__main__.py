import argparse
import json
import os
import tempfile
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analysis import analyze, render_text
from .report import render
from .usage import scan


def main():
    parser = argparse.ArgumentParser(description="Claude Code / Codexのローカル利用状況を集計")
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects",
        help="Claudeのprojectsディレクトリ",
    )
    parser.add_argument(
        "--codex-dir",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
        help="Codexホーム（sessions等の親）",
    )
    parser.add_argument("--days", type=int, default=30, help="当日を含む日数（0は全期間、既定30）")
    parser.add_argument("--timezone", help="日別集計のIANAタイムゾーン（既定はOSのローカル時刻）")
    parser.add_argument("--json", action="store_true", help="HTMLの代わりにJSONを出力（既定は標準出力）")
    parser.add_argument("--output", type=Path, help="出力先（HTMLの既定はreport.html）")
    parser.add_argument("--open", action="store_true", help="生成したHTMLをブラウザで開く")
    parser.add_argument("--analyze", action="store_true", help="優先セッション・原因候補・最初に試す改善を文章で出力（--jsonで分析JSON）")
    parser.add_argument("--from-report", type=Path, help="分析用の保存済み集計JSON。期間は保存された集計のまま、元ログは読み込まない")
    parser.add_argument("--top", type=int, default=5, help="分析で選ぶ最大セッション数（既定5）")
    parser.add_argument("--min-tokens", type=int, default=100_000, help="分析候補の最低総トークン数（既定100000）")
    args = parser.parse_args()
    if args.days < 0:
        parser.error("--days は0以上を指定してください")
    if args.open and args.json:
        parser.error("--open と --json は同時に指定できません")
    if args.open and args.analyze:
        parser.error("--analyze は文章またはJSON出力です。--open はHTML生成時に指定してください")
    if args.from_report and not args.analyze:
        parser.error("--from-report は --analyze と一緒に指定してください")
    if args.top < 1 or args.min_tokens < 0:
        parser.error("--top は1以上、--min-tokens は0以上を指定してください")
    try:
        tz = ZoneInfo(args.timezone) if args.timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        parser.error("指定したタイムゾーンが見つかりません")
    if args.from_report:
        try:
            data = json.loads(args.from_report.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            parser.error(f"集計JSONを読み込めません: {exc}")
    else:
        data = scan(args.claude_dir.expanduser(), args.codex_dir.expanduser(), args.days, tz)
    if args.analyze:
        try:
            analysis = analyze(data, args.top, args.min_tokens)
        except ValueError as exc:
            parser.error(str(exc))
        content = json.dumps(analysis, ensure_ascii=False, indent=2) if args.json else render_text(analysis)
    else:
        content = json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data)
    if (args.json or args.analyze) and args.output is None:
        print(content)
        return
    destination = (args.output or Path("report.html")).expanduser().resolve()
    if args.from_report and destination == args.from_report.expanduser().resolve():
        parser.error("入力した集計JSONと同じファイルには出力できません")
    # Reports must never overwrite the source transcripts, even with a custom output.
    log_roots = (args.claude_dir, args.codex_dir / "sessions", args.codex_dir / "archived_sessions")
    for source in log_roots:
        root = source.expanduser().resolve()
        if destination == root or root in destination.parents:
            parser.error("ログの保存先には出力できません")
    temporary = None
    try:
        # Replacing a new inode also preserves transcripts reached via hard links.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, destination)
    except OSError as exc:
        parser.exit(1, f"出力に失敗しました: {exc}\n")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"保存しました: {destination}")
    if args.open:
        webbrowser.open(destination.as_uri())


if __name__ == "__main__":
    main()
