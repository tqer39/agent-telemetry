"""Self-contained HTML: no CDN, scripts, network requests or raw transcripts."""

from html import escape

from .analysis import analyze
from .efficiency import digest

NAMES = {"claude": "Claude Code", "codex": "Codex"}
FORMATS = {
    "claude_response": "Claude応答単位",
    "codex_response": "Codex応答単位",
    "codex_cumulative_delta": "Codex旧形式・累積増分（呼出回数は不明）",
}


def render(data):
    def e(value):
        return escape(str(value))

    def n(value):
        return f"{value:,}"

    cards = []
    for provider, values in data["providers"].items():
        source = next(s for s in data["sources"] if s["provider"] == provider)
        cards.append(f"""<section class="card"><h2>{NAMES[provider]}</h2>
        <div class="big">{n(values["input"] + values["output"])}<small> tokens</small></div>
        <dl><dt>セッション</dt><dd>{n(values["sessions"])}</dd>
        <dt>使用量記録</dt><dd>{n(values["requests"])}</dd>
        <dt>入力（キャッシュを含む）</dt><dd>{n(values["input"])}</dd>
        <dt>出力（推論を含む）</dt><dd>{n(values["output"])}</dd>
        <dt>キャッシュ読取 / 書込</dt><dd>{n(values["cache_read"])} / {n(values["cache_write"])}</dd>
        <dt>期間内の使用量記録なし</dt><dd>{n(values["sessions_without_usage"])} セッション</dd></dl>
        <p class="muted">最終活動: {e(values["last_activity"] or "記録なし")}<br>

        ログ: {source["read_files"]} / {source["files"]} ファイル読取
        （圧縮: {source["compressed_files"]}）{" / 未検出" if not source["found"] else ""}</p></section>""")

    def table(rows, headings, keys):
        body = []
        for row in rows:
            cells = []
            for key in keys:
                v = row[key]
                cells.append(f"<td>{e(NAMES.get(v, v) if key == 'provider' else n(v) if isinstance(v, int) else v)}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        if not body:
            body.append(f'<tr><td colspan="{len(keys)}">この期間の使用量記録はありません。</td></tr>')
        return (
            '<div class="scroll"><table><thead><tr>'
            + "".join(f"<th>{h}</th>" for h in headings)
            + "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table></div>"
        )

    daily = table(
        data["daily"],
        ["日付", "ツール", "使用量記録", "入力", "出力", "キャッシュ読取"],
        ["date", "provider", "requests", "input", "output", "cache_read"],
    )
    models = table(data["models"], ["ツール", "モデル", "使用量記録", "入力", "出力"], ["provider", "model", "requests", "input", "output"])
    session_rows = data.get("sessions", [])
    total = sum(s["total"] for s in session_rows)
    analysis = analyze(data) if "sessions" in data else None
    recommendations = []
    for rank, item in enumerate(analysis["recommendations"] if analysis else [], 1):
        causes = "".join(f"<li>{e(c['title'])}: {e(c['evidence'])}</li>" for c in item["causes"])
        action = item["first_action"]
        recommendations.append(f"""<article class="card"><h3>{rank}. <a href="#{e(item["anchor"])}">{e(item["session_id"])}</a></h3>
        <p class="wrap">{e(item["repository"])} / {e(item["provider"])}<br>
{n(item["total"])} tokens（総消費の {item["share"]:.1%}）</p>
        <p>選定理由: {e(item["selection_reason"])}</p><ul>{causes}</ul>
        <h4>最初に試す: {e(action["title"])}</h4><p>{e(action["steps"])}</p><p>検証: {e(action["verify"])}</p>
        <p class="muted">{e(item["caveat"])} 詳細の元ログで確認してください。</p></article>""")
    automatic = f"""<section><h2>優先して試す改善（自動分析・LLM不要）</h2>
    <p>総量10万tokens以上かつ改善候補があるセッションから最大5件を選びます。総量×兆候の重みで確認順を決め、削減可能量や効率の点数は推定しません。</p>
    <p class="muted">重み: 反復3、エラー候補2、大きな出力2、入力増加1、低キャッシュ1。
    反復＋エラー候補3件以上、大きな出力＋入力増加の組ごとに+1。
    反復の半数以上かつ3回以上が既知の待機なら反復の重みは1、エラーとの加点なし。組の同時発生は因果関係を証明しません。</p>
    {"".join(recommendations) or "<p>この閾値・検出条件では候補なし。効率や品質が良いという判定ではありません。</p>"}</section>"""

    def result_status(error):
        if error is None:
            return "不明"
        return "エラー・非ゼロ" if error else "成功フラグ・ゼロ終了"

    def pct(value):
        return f"{value:.1%}" if value is not None else "不明"

    def value(number):
        return f"{number:,.0f}" if number is not None else "不明"

    def sparkline(s):
        points = s["input_timeline"]
        if not points:
            return '<p class="muted">使用量記録がなく、入力推移は不明です。</p>'
        peak = max((p["input"] for p in points), default=0) or 1
        last = max(s["requests"] - 1, 1)

        def line(fresh=False):
            coordinates = []
            for point in points:
                amount = max(0, point["input"] - point["cache_read"]) if fresh else point["input"]
                x = (point["record"] - 1) / last * 600
                y = 100 - amount / peak * 90
                coordinates.append(f"{x:.1f},{y:.1f}")
            return " ".join(coordinates)

        fresh_line = f'<polyline points="{line(True)}" fill="none" stroke="#ffbb88" stroke-width="2"/>' if s["cache_known"] else ""
        return f'''<figure><svg viewBox="0 0 600 110" role="img" aria-label="記録順の入力トークン推移">
        <title>入力トークンの推移。縦軸のサンプル最大値 {peak:,}、横軸は記録順。</title>
        <polyline points="{line()}" fill="none" stroke="#88dfcb" stroke-width="2"/>{fresh_line}</svg>
        <figcaption class="muted">入力推移: 緑＝入力、橙＝キャッシュ読取を除く入力（判明時）。
        {s["requests"]} 記録から {len(points)} 点を等間隔抽出、縦軸最大 {peak:,}。
        省略したピークは下の消費上位記録で確認。</figcaption></figure>'''

    ranking_rows = []
    for s in session_rows[:20]:
        ranking_rows.append(f"""<tr><td><a href="#{s["anchor"]}">{e(s["session_id"])}</a><br>
<small>{NAMES[s["provider"]]}</small></td>
        <td class="wrap">{e(s["repository"])}</td><td>{n(s["total"])}<br>
<small>{pct(s["total"] / total if total else None)}</small></td>
        <td>{pct(s["cache_read_rate"])}</td><td>{value(s["input_per_record"])}</td>
        <td>{len(s["hints"])} 件<br>
<small>{e(s["status"])}</small></td></tr>""")
    ranking = (
        (
            '<div class="scroll"><table><thead><'
            "tr><th>セッション</th><th>リポジトリ / 作業場所</"
            "th><th>総tokens / 構成比</th><th>キャッシュ読"
            "取率</th><th>入力 / 記録</th><th>確認候補</th"
            "></tr></thead><tbody>"
        )
        + "".join(ranking_rows)
        + "</tbody></table></div>"
        if ranking_rows
        else "<p>期間内のセッションはありません。</p>"
    )
    repo_rows = []
    details = []
    for repo in data.get("repositories", []):
        group = [s for s in session_rows if s["repository_key"] == repo["repository_key"]]
        if not group:
            continue
        anchor = "repo-" + digest(group[0]["repository_key"])
        repo_rows.append(f"""<tr><td class="wrap"><a href="#{anchor}">{e(repo["repository"])}</a><br>
<small>{e(repo["repository_basis"])}</small></td>
        <td>{repo["sessions"]}</td><td>{n(repo["input"] + repo["output"])}</td><td>{repo["hints"]}</td></tr>""")
        details.append(f'<h3 id="{anchor}" class="wrap">{e(repo["repository"])}</h3>')
        for s in group:
            hints = "".join(
                f"""<li><strong>{e(h["title"])}</strong> <small>原因候補・要確認</small><br>

                     観測: {e(h["evidence"])}<br>
改善のヒント: {e(h["suggestion"])}</li>"""
                for h in s["hints"]
            )
            if not hints:
                hints = (
                    "<li>検出した改善候補はありません。成果・品質や、効率が良いことを保証するものではありません。</li>"
                    if s["requests"]
                    else "<li>使用量の記録がないため、効率を判断できません。</li>"
                )
            hotspots = []
            for h in s["hotspots"]:
                hotspots.append(f"""<tr><td>{e(h["timestamp"])}</td><td>{n(h["input"])}</td><td>{n(h["output"])}</td>
                <td>{n(h["cache_read"])}</td><td class="wrap">{e(h["model"])}<br>
直前の呼出: {e(", ".join(h["preceding_tools"]) or "記録なし")}</td>
                <td class="wrap">{e(h["source"])}:{h["line"]}</td></tr>""")
            hotspot_table = (
                (
                    '<div class="scroll"><table><thead><'
                    "tr><th>日時</th><th>入力</th><th>出力</th"
                    "><th>キャッシュ読取</th><th>モデル / 周辺のツール</"
                    "th><th>元ログ:行</th></tr></thead><tbod"
                    "y>"
                )
                + "".join(hotspots)
                + "</tbody></table></div>"
                if hotspots
                else "<p>使用量記録なし</p>"
            )
            tools = "".join(
                f"""<li>{e(t["tool"])}: 結果 {n(t["output_chars"])} 文字 / 終了状態 {result_status(t["error"])}<br>

                    <small class="wrap">{e(t["timestamp"])} / {e(t["source"])}:{t["line"]}</small></li>"""
                for t in s["tool_evidence"]
            )
            repeated = "".join(
                f"""<li>{e(g["tool"])}: 同一引数で {g["calls"]} 回（追加 {g["calls"] - 1} 回）<br>

                       <small class="wrap">最初: {e(g["first_source"])}:{g["first_line"]}<br>
最後: {e(g["last_source"])}:{g["last_line"]}</small></li>"""
                for g in s["repeated_tool_groups"]
            )
            details.append(f'''<details id="{s["anchor"]}"><summary>
{NAMES[s["provider"]]} · {e(s["session_id"])} · {n(s["total"])} tokens · 確認候補 {len(s["hints"])} 件
</summary>
            <div class="detail"><p>{e(s["status"])}。{e(s["outcome"])}。</p>
            <p class="muted wrap">作業場所: {e(s["cwd"] or "不明")}<br>
リポジトリ特定の根拠: {e(s["repository_basis"])}<br>
最終活動: {e(s["last_activity"])}<br>
記録単位: {e(", ".join(FORMATS.get(f, f) for f in s["usage_formats"]) or "使用量なし")}<br>
モデル: {e(", ".join(s["models"]) or "不明")}</p>
            <dl><dt>入力 / 出力</dt><dd>{n(s["input"])} / {n(s["output"])}</dd>
            <dt>キャッシュ読取率</dt><dd>{pct(s["cache_read_rate"])}</dd>
            <dt>キャッシュ読取を除く入力</dt><dd>{value(s["non_cached_input"])}</dd>
            <dt>入力 / 使用量記録</dt><dd>{value(s["input_per_record"])}</dd>
            <dt>先頭1/3 → 末尾1/3の入力中央値</dt><dd>{value(s["first_input_median"])} → {value(s["last_input_median"])}</dd>
            <dt>入力の増加倍率（6記録以上）</dt><dd>{f"{s['input_growth']:.1f}倍" if s["input_growth"] is not None else "不明"}</dd>
            <dt>ツール呼出 / 同一引数の追加実行</dt><dd>{s["tool_calls"]} / {s["repeated_tool_calls"]}</dd>
            <dt>結果の終了状態が判明 / 結果数</dt><dd>{s["known_result_status"]} / {s["tool_results"]}</dd>
            <dt>コンパクション記録</dt><dd>{s["compactions"]}</dd></dl>
            {sparkline(s)}<h4>観測事実と改善のヒント</h4><ul class="hints">{hints}</ul>
            <h4>消費の大きい記録（上位10件）</h4>
            <p class="muted">直前のツールは周辺情報で、消費の原因を証明しません。行番号は展開後のJSONLです。
ストリーミング応答の使用量は複数記録を統合し、代表行を表示します。</p>{hotspot_table}
            <h4>同一引数の反復（回数上位10組）</h4><ul>{repeated or "<li>観測できた反復なし</li>"}</ul>
            <h4>ツール結果の確認先（エラー候補・大きな出力から各最大10件）</h4>
            <ul>{tools or "<li>記録なし。ツールを使用していないことを意味しません。</li>"}</ul>
            <p class="muted wrap">読取元: {e(" / ".join(s["sources"]))}</p></div></details>''')
    repo_table = (
        (
            '<div class="scroll"><table><thead><'
            "tr><th>リポジトリ / 作業場所</th><th>セッション</"
            "th><th>総tokens</th><th>確認候補</th></t"
            "r></thead><tbody>"
        )
        + "".join(repo_rows)
        + "</tbody></table></div>"
    )
    efficiency = f"""<section><h2>トークン効率の確認</h2><aside>消費量の多さだけで非効率とは判断しません。成果・完了・品質は未評価です。
    ヒントは固定の検出条件による確認候補です。記録不足は「不明」とし、問題なしとは判定しません。<br>

    キャッシュ読取率＝キャッシュ読取 / 入力。キャッシュ書込を含む非読取入力は、請求額や無駄なトークン量を表すものではありません。<br>

    同一引数の反復は、監視・再検証など必要な作業でも発生します。ツール結果の文字数は入力トークン数とは異なります。</aside>
    <h3>消費の大きいセッション（上位20件）</h3>{ranking}
    <h3>リポジトリ別の消費</h3>{repo_table}
    <h3>セッション詳細（リポジトリ別・消費順）</h3>{"".join(details)}</section>"""
    labels = {
        "invalid_lines": "不正・書込途中の行",
        "unreadable_files": "読取不可ファイル",
        "unsupported_files": "未対応形式のファイル",
        "usage_without_timestamp": "日時不明の使用量",
        "corrupt_compressed_files": "破損または読取不可の圧縮ファイル（全体を除外）",
        "zstd_unavailable_files": "Zstandard未読取（compression.zstd対応のPython 3.14以降が必要）",
        "duplicate_files": "同名ログの圧縮版を重複除外",
    }
    warnings = " / ".join(f"{labels.get(k, k)}: {v}" for k, v in data["diagnostics"].items()) or "読取エラーなし"
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Agent Telemetry — ローカル利用状況</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#10141d;color:#e8edf5}}
body{{max-width:1100px;margin:auto;padding:40px 24px}}h1{{font-size:32px;margin-bottom:8px}}
h2{{font-size:20px}}.muted,small{{color:#aab9cc;font-size:13px;overflow-wrap:anywhere}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px;margin:28px 0}}
.card{{background:#1b2331;border:1px solid #344057;border-radius:16px;padding:24px}}
.big{{font-size:36px;color:#88dfcb;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
dl{{display:grid;grid-template-columns:1fr auto;gap:12px;font-size:14px}}dd{{margin:0;text-align:right}}
.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:32px}}
th,td{{text-align:left;padding:12px;border-bottom:1px solid #344057;white-space:nowrap}}
th{{color:#aab9cc}}aside{{border-left:3px solid #88dfcb;padding:8px 16px;line-height:1.8}}
a{{color:#88dfcb}}details{{border:1px solid #344057;border-radius:10px;margin:10px 0}}
summary{{cursor:pointer;padding:16px;overflow-wrap:anywhere}}
.detail{{padding:0 20px 20px}}.wrap{{white-space:normal;overflow-wrap:anywhere}}.hints li{{margin:16px 0;line-height:1.8}}
figure{{margin:20px 0}}svg{{width:100%;max-height:140px}}h3{{margin-top:30px}}
</style></head><body><p class="muted">LOCAL USAGE / 読み取り専用スナップショット</p>
<h1>エージェントの利用状況</h1><p class="muted">{e(data["start_date"] or "全期間")} 〜 {e(data["end_date"])} / {e(data["timezone"])}<br>

生成日時: {e(data["generated_at"])}（更新するにはコマンドを再実行）</p>
<div class="grid">{"".join(cards)}</div>{automatic}{efficiency}<h2>日別の使用量</h2>{daily}<h2>モデル別</h2>{models}
<aside>入力・出力の合計が総トークンです。キャッシュと推論は内数のため加算しません。<br>

使用量記録はClaudeでは応答単位、Codexの旧形式では累積値の増分単位で、厳密なAPI呼出回数ではありません。<br>

ローカルに残るログのみを対象とし、契約の残量・請求額やClaude Desktopの通常チャットは含みません。</aside>
<p class="muted">{e(warnings)}</p></body></html>"""
