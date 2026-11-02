"""Deterministic triage from aggregate metadata; no model or network dependency."""

WEIGHTS = {"repeated_tools": 3, "tool_errors": 2, "large_tool_output": 2, "input_growth": 1, "low_cache": 1}
ACTIONS = {
    "repeated_tools": (
        "反復している調査・監視を一組確認する",
        "同一引数の最初と最後の呼出を比較する。結果が変わらない調査なら結果を引き継ぎ、ポーリングなら間隔を延ばす。変更後の再検証は維持する。",
        "同じ目的・完了条件の作業で、必要な検証を維持しつつ同一引数の追加実行と総入力が減るか確認する。",
    ),
    "tool_errors": (
        "実行条件と非ゼロ終了の意味を確認する",
        "エラー候補の元ログを読み、正常な検索の該当なし等を除外する。実際の失敗なら依存関係・権限・引数を先に整えてから再実行する。",
        "同じ検証が通ることを確認し、実際の失敗とそれに続く再実行が減るか比較する。",
    ),
    "large_tool_output": (
        "最も大きいツール出力を絞る",
        "元ログでその出力が必要だったか確認する。検索対象・読取範囲・テストログを絞り、必要な結果と検証証拠を残す。",
        "同じ作業・モデル・完了条件で、最大出力文字数と入力/記録が減るか確認する。出力文字数を削減トークン数へ換算しない。",
    ),
    "input_growth": (
        "入力が増えた区間を確認して作業を区切る",
        "入力上位の記録を確認する。独立した作業へ移ったなら目的・決定・未完了項目・検証結果を引き継いで会話を分ける。",
        "前提の再説明や手戻りが増えないことを確認し、同程度の作業で末尾の入力中央値と総入力を比較する。",
    ),
    "low_cache": (
        "固定の前提・指示の変化を確認する",
        "同じモデルで繰り返す作業について、固定の指示や前提が毎回変わっていないか確認する。新規作業で低い読取率を無理に改善しない。",
        "同程度の作業でキャッシュ読取率と非読取入力を比較する。率だけでなく総入力・成果・品質も確認する。",
    ),
}
WAIT_TOOLS = {"sleep", "clock.sleep", "wait", "functions.wait", "wait_agent", "collaboration.wait_agent", "wait_threads"}


def validate(data):
    """Reject incompatible snapshots with a useful error before analysis."""
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise ValueError("セッション情報を含む集計JSONが必要です。最新版で --json を再生成してください")
    for session in data["sessions"]:
        if not isinstance(session, dict):
            raise ValueError("sessions の各要素はオブジェクトである必要があります")
        for key in ("total", "requests", "input", "output"):
            if type(session.get(key)) is not int or session[key] < 0:
                raise ValueError(f"セッションの {key} が不正です")
        for key in ("session_id", "provider", "repository", "anchor"):
            if not isinstance(session.get(key), str):
                raise ValueError(f"セッションの {key} が不正です")
        if session["total"] != session["input"] + session["output"]:
            raise ValueError("セッションの total と input + output が一致しません")
        if not isinstance(session.get("hints"), list):
            raise ValueError("セッションの hints が不正です")
        for hint in session["hints"]:
            if not isinstance(hint, dict) or any(not isinstance(hint.get(k), str) for k in ("code", "title", "evidence")):
                raise ValueError("ヒントの code / title / evidence が不正です")
        for key in ("repeated_tool_groups", "tool_evidence", "hotspots"):
            if not isinstance(session.get(key, []), list) or any(not isinstance(v, dict) for v in session.get(key, [])):
                raise ValueError(f"セッションの {key} が不正です")
        for key in ("repeated_tool_calls", "tool_errors"):
            if type(session.get(key, 0)) is not int or session.get(key, 0) < 0:
                raise ValueError(f"セッションの {key} が不正です")
        rate = session.get("cache_read_rate")
        if rate is not None and (type(rate) not in (float, int) or not 0 <= rate <= 1):
            raise ValueError("セッションの cache_read_rate が不正です")
        for g in session.get("repeated_tool_groups", []):
            if not isinstance(g.get("tool"), str) or type(g.get("calls")) is not int or g["calls"] < 1:
                raise ValueError("反復グループの tool / calls が不正です")
        for r in session.get("tool_evidence", []):
            if type(r.get("output_chars", 0)) is not int or r.get("output_chars", 0) < 0:
                raise ValueError("ツール結果の output_chars が不正です")


def analyze(data, top=5, min_tokens=100_000):
    validate(data)
    if type(top) is not int or type(min_tokens) is not int or top < 1 or min_tokens < 0:
        raise ValueError("top は1以上、min_tokens は0以上が必要です")
    sessions = data["sessions"]
    total = sum(s["total"] for s in sessions)
    candidates = []
    for s in sessions:
        hints = {h["code"]: dict(h) for h in s["hints"] if h["code"] in WEIGHTS}
        if not s["requests"] or s["total"] < min_tokens or not hints:
            continue
        weights = dict(WEIGHTS)
        wait_repeats = sum(g["calls"] - 1 for g in s.get("repeated_tool_groups", []) if g["tool"] in WAIT_TOOLS)
        wait_heavy = wait_repeats >= 3 and wait_repeats >= s.get("repeated_tool_calls", 0) / 2
        if wait_heavy and "repeated_tools" in hints:
            weights["repeated_tools"] = 1
            hints["repeated_tools"]["evidence"] += (
                f"。少なくとも {wait_repeats} 回は待機・状態確認の反復で、再調査とは区別（反復の重みを3→1）"
            )
        weight = sum(weights[code] for code in hints)
        paired_output = "input_growth" in hints and "large_tool_output" in hints
        paired_retry = "repeated_tools" in hints and "tool_errors" in hints and s.get("tool_errors", 0) >= 3 and not wait_heavy
        weight += int(paired_output) + int(paired_retry)
        if paired_retry:
            first = "tool_errors"
            reason = (
                "反復とエラー候補が同じセッションにあるため、不要な再実行の前提になって"
                "いる失敗がないかを先に確認する。両者の因果関係は未確認。"
            )
        elif paired_output:
            first = "large_tool_output"
            reason = "大きな出力と入力増加が同じセッションにあるため、出力の絞り込みを先に試す。両者の因果関係は未確認。"
        else:
            first = sorted(hints, key=lambda c: (-weights[c], c))[0]
            reason = "検出した候補のうち、定義した重みが最大の項目を最初の確認対象にする。"
        action, steps, verify = ACTIONS[first]
        if first == "repeated_tools" and wait_heavy:
            action = "待機後の状態確認の頻度を確認する"
            steps = (
                "待機自体を削るのではなく、状態確認ごとにモデルの再呼出が起きているか元"
                "ログで確認する。必要な監視を維持したまま確認間隔を延ばせるか試す。"
            )
        refs = []
        if first == "repeated_tools":
            groups = s.get("repeated_tool_groups", [])
            if not wait_heavy:
                groups = [g for g in groups if g["tool"] not in WAIT_TOOLS] or groups
            for group in groups[:2]:
                refs.extend(
                    {
                        "source": group.get(f"{edge}_source"),
                        "line": group.get(f"{edge}_line"),
                        "label": f"{group.get('tool', 'unknown')} / 同一引数 {group.get('calls', 0)} 回 / {edge}",
                    }
                    for edge in ("first", "last")
                )
        elif first in ("large_tool_output", "tool_errors"):
            results = s.get("tool_evidence", [])
            results = (
                [r for r in results if r.get("error") is True]
                if first == "tool_errors"
                else sorted(results, key=lambda r: r.get("output_chars", 0), reverse=True)
            )
            refs = [
                {
                    "source": r.get("source"),
                    "line": r.get("line"),
                    "label": f"{r.get('tool', 'unknown')} / 出力 {r.get('output_chars', 0):,} 文字",
                }
                for r in results[:2]
            ]
        if not refs:
            refs = [
                {"source": h.get("source"), "line": h.get("line"), "label": f"消費上位記録 / {h.get('timestamp', '日時不明')}"}
                for h in s.get("hotspots", [])[:2]
            ]
        refs = [r for r in refs if isinstance(r["source"], str) and type(r["line"]) is int and r["line"] > 0]
        candidates.append(
            {
                "session_id": s["session_id"],
                "provider": s["provider"],
                "repository": s["repository"],
                "anchor": s["anchor"],
                "total": s["total"],
                "input": s["input"],
                "output": s["output"],
                "requests": s["requests"],
                "share": s["total"] / total if total else 0,
                "cache_read_rate": s.get("cache_read_rate"),
                "priority_weight": weight,
                "priority_index": s["total"] * weight,
                "selection_reason": f"総量 {s['total']:,} × 兆候の重み {weight}。" + reason,
                "causes": [
                    dict(code=c, title=hints[c]["title"], evidence=hints[c]["evidence"], weight=weights[c])
                    for c in sorted(hints, key=lambda c: (-weights[c], c))
                ],
                "known_wait_repeats": wait_repeats,
                "wait_heavy": wait_heavy,
                "first_action": {"code": first, "title": action, "steps": steps, "verify": verify},
                "references": refs,
                "caveat": "確認候補であり、非効率・削減量・成果との因果関係は未確定。",
            }
        )
    candidates.sort(key=lambda c: (-c["priority_index"], -c["total"], c["provider"], c["session_id"]))
    selected = candidates[:top]
    return {
        "method": "rules-v1",
        "generated_at": data.get("generated_at"),
        "period": {k: data.get(k) for k in ("start_date", "end_date", "timezone")},
        "parameters": {"top": top, "min_tokens": min_tokens},
        "policy": (
            "総量が閾値以上で既知の改善候補があるセッションを、総量×兆候の重みの降"
            "順で選ぶ。同点は総量・ツール・ID順。指数は確認順で、削減トークン数や"
            "効率の点数ではない。"
        ),
        "weights": dict(WEIGHTS),
        "pair_bonus": (
            "大きな出力＋入力増加、または反復＋エラー候補3件以上の組ごとに重み+1"
            "。反復の半数以上かつ3回以上が既知の待機なら反復の重みは1、エラーとの"
            "加点なし（因果関係は未確認）"
        ),
        "sessions": len(sessions),
        "candidate_sessions": len(candidates),
        "usage_missing_sessions": sum(not s["requests"] for s in sessions),
        "selected_total": sum(s["total"] for s in selected),
        "selected_share": sum(s["total"] for s in selected) / total if total else 0,
        "diagnostics": data.get("diagnostics", {}),
        "recommendations": selected,
    }


def render_text(analysis):
    period = analysis["period"]
    lines = [
        "トークン消費の改善候補分析（LLM不要 / rules-v1）",
        f"期間: {period['start_date'] or '全期間'} 〜 {period['end_date']} / {period['timezone']}",
        f"集計生成日時: {analysis['generated_at']}",
        f"対象 {analysis['sessions']} セッション / 候補 {analysis['candidate_sessions']} / 使用量不明 {analysis['usage_missing_sessions']}",
        f"選出分の総消費に占める割合: {analysis['selected_share']:.1%}（削減可能量ではない）",
        "選定: " + analysis["policy"],
        "重み: " + ", ".join(f"{k}={v}" for k, v in analysis["weights"].items()),
        analysis["pair_bonus"],
        "",
    ]
    if not analysis["recommendations"]:
        lines.append("指定した閾値・検出条件では候補がありません。効率や品質が良いという判定ではありません。")
    for rank, item in enumerate(analysis["recommendations"], 1):
        lines.extend(
            [
                f"{rank}. {item['repository']} / {item['provider']} / {item['session_id']}",
                f"   消費: {item['total']:,} tokens（構成比 {item['share']:.1%}、入力 {item['input']:,} / 出力 {item['output']:,}）",
                "   キャッシュ読取率: "
                + (f"{item['cache_read_rate']:.1%}（金額・無駄な量は未推定）" if item["cache_read_rate"] is not None else "不明"),
                "   選定理由: " + item["selection_reason"],
            ]
        )
        for cause in item["causes"]:
            lines.append(f"   原因候補: {cause['title']} — 観測: {cause['evidence']}")
        action = item["first_action"]
        lines.extend(["   最初に試す: " + action["title"], "   手順: " + action["steps"], "   検証: " + action["verify"]])
        for ref in item["references"]:
            lines.append(f"   確認先: {ref['source']}:{ref['line']}（{ref['label']}）")
        if not item["references"]:
            lines.append("   確認先: 元ログ参照の記録なし。最新版で集計を再生成してください。")
        lines.extend(["   留意点: " + item["caveat"], ""])
    if analysis["diagnostics"]:
        lines.append("集計時の除外・注意事項: " + str(analysis["diagnostics"]))
    lines.append("成果・完了・品質は未評価。同程度の目的・モデル・記録形式・完了条件で、改善策を一つずつ試して比較してください。")
    return "\n".join(lines) + "\n"
