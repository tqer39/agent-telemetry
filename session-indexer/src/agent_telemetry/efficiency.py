"""Evidence-based session analysis. No prompts, commands or result bodies are exported."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from contextlib import suppress
from datetime import UTC
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def remote_name(remote):
    if not isinstance(remote, str):
        return None
    try:
        if "://" in remote:
            parsed = urlsplit(remote)
            if parsed.scheme not in ("https", "http", "ssh", "git") or not parsed.hostname:
                return None
            name = parsed.hostname.lower() + "/" + parsed.path.strip("/")
        else:
            match = re.fullmatch(r"(?:[^/@\s]+@)?([\w.-]+):([^\s?#]+)", remote)
            if not match:
                return None
            name = match[1].lower() + "/" + match[2].strip("/")
        return name.removesuffix(".git")
    except ValueError:
        return None


@lru_cache(maxsize=2048)
def repository(cwd, recorded_remote=None):
    """Use Git's common directory so linked worktrees share a repository identity."""
    recorded = remote_name(recorded_remote)
    if recorded:
        return {"key": f"remote:{recorded}", "label": recorded, "basis": "ログ記録のリポジトリURL"}
    if not cwd or not Path(cwd).is_dir():
        return {"key": f"cwd:{cwd or 'unknown'}", "label": cwd or "不明", "basis": "作業ディレクトリ（Git未確認）"}
    # Inherited Git environment must not redirect discovery into the caller's repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        git = shutil.which("git")
        if git is None:
            raise FileNotFoundError("git")
        result = subprocess.run(  # noqa: S603 -- fixed Git query; cwd is one argument
            [git, "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
            check=False,
        )
        common = result.stdout.strip()
        if result.returncode == 0 and Path(common).is_absolute():
            origin = subprocess.run(  # noqa: S603 -- fixed Git config query without a shell
                [git, "-C", cwd, "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=3, env=env, check=False
            )
            remote = remote_name(origin.stdout.strip()) if origin.returncode == 0 else None
            if remote:
                return {"key": f"remote:{remote}", "label": remote, "basis": "現在のローカルGit origin（過去の所属は未検証）"}
            label = str(Path(common).parent) if Path(common).name == ".git" else common
            return {"key": f"git:{common}", "label": label, "basis": "現在のローカルGit共通ディレクトリ"}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"key": f"cwd:{cwd}", "label": cwd, "basis": "作業ディレクトリ（Git未確認）"}


def error_status(output):
    """Only explicit flags/exit codes count; the word 'error' alone is insufficient."""
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            return error_status(parsed)
        match = re.search(r"(?m)^(?:Process exited with code|Exit code:)\s*(-?\d+)\s*$", output)
        return int(match[1]) != 0 if match else None
    if isinstance(output, dict):
        if type(output.get("is_error")) is bool:
            return output["is_error"]
        for k in ("exit_code", "exitCode"):
            if type(output.get(k)) is int:
                return output[k] != 0
        if isinstance(output.get("metadata"), dict):
            return error_status(output["metadata"])
    return None


class Trace:
    def __init__(self, path, provider, in_range):
        self.path = str(path)
        self.provider = provider
        self.in_range = in_range
        self.cwd = None
        self.recorded_remote = None
        self.calls = {}
        self.results = {}
        self.events = {}

    def observe(self, row, dt, line):
        payload = row.get("payload")
        p = payload if isinstance(payload, dict) else {}
        cwd = row.get("cwd") if self.provider == "claude" else p.get("cwd")
        if isinstance(cwd, str) and self.cwd is None:
            self.cwd = cwd
        if row.get("type") == "session_meta":
            git = p.get("git")
            if isinstance(git, dict) and isinstance(git.get("repository_url"), str):
                self.recorded_remote = git["repository_url"]
        if not dt or not self.in_range(dt):
            return
        kind = row.get("type")
        base = {"timestamp": dt.astimezone(UTC).isoformat(), "source": self.path, "line": line}

        def call(cid, name, arguments):
            if not isinstance(cid, str) or not isinstance(name, str):
                return
            if isinstance(arguments, str):
                with suppress(ValueError, RecursionError):
                    arguments = json.loads(arguments)
            fingerprint = digest(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False))
            self.calls.setdefault(cid, dict(base, name=name, fingerprint=fingerprint))

        def result(cid, output, flag=None):
            if not isinstance(cid, str):
                return
            status = flag if type(flag) is bool else error_status(output)
            size = len(output) if isinstance(output, str) else len(json.dumps(output, ensure_ascii=False))
            self.results[cid] = dict(base, error=status, output_chars=size)

        if self.provider == "claude":
            m = row.get("message")
            m = m if isinstance(m, dict) else {}
            content = m.get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    call(b.get("id"), b.get("name"), b.get("input"))
                elif b.get("type") == "tool_result":
                    result(b.get("tool_use_id"), b.get("content"), b.get("is_error"))
            if kind == "system" and row.get("subtype") == "compact_boundary":
                self.events[row.get("uuid") or digest(base)] = dict(base, kind="compaction")
        elif kind == "response_item":
            ty = p.get("type")
            if ty in ("function_call", "custom_tool_call"):
                name = p.get("name")
                namespace = p.get("namespace")
                if isinstance(namespace, str) and isinstance(name, str):
                    name = f"{namespace}.{name}"
                call(p.get("call_id"), name, p.get("arguments", p.get("input")))
            elif ty in ("function_call_output", "custom_tool_call_output"):
                result(p.get("call_id"), p.get("output"))
        elif kind == "compacted":
            self.events[digest([dt, line])] = dict(base, kind="compaction")

    def export(self):
        return {
            "cwd": self.cwd,
            "recorded_remote": self.recorded_remote,
            "calls": self.calls,
            "results": self.results,
            "events": self.events,
        }


def summarize(session, records, trace, tz):
    """Rates describe observed usage, not quality or monetary savings."""
    ordered = sorted(records, key=lambda r: (r[2], r[5]["line"]))
    counts = session
    total = counts["input"] + counts["output"]
    inputs = [r[4]["input"] for r in ordered]
    cache_known = all(r[5]["cache_known"] for r in ordered) and bool(ordered)
    input_total = counts["input"]
    cache_rate = counts["cache_read"] / input_total if input_total and cache_known else None
    fresh = max(0, input_total - counts["cache_read"]) if cache_known else None
    split = max(1, len(inputs) // 3)
    first = median(inputs[:split]) if inputs else None
    last = median(inputs[-split:]) if inputs else None
    growth = last / first if len(inputs) >= 6 and first else None
    calls, results = trace["calls"], trace["results"]
    fingerprints = Counter(c["fingerprint"] for c in calls.values())
    repeats = sum(n - 1 for n in fingerprints.values())
    failures = sum(r["error"] is True for r in results.values())
    known = sum(r["error"] is not None for r in results.values())
    compactions = sum(e["kind"] == "compaction" for e in trace["events"].values())
    observations = []

    def hint(code, title, evidence, suggestion):
        observations.append(
            {"code": code, "title": title, "evidence": evidence, "suggestion": suggestion, "classification": "原因候補・要確認"}
        )

    if cache_rate is not None and input_total >= 100_000 and cache_rate < 0.5:
        hint(
            "low_cache",
            "キャッシュ再利用が少ない",
            f"入力 {input_total:,} のうちキャッシュ読取 {counts['cache_read']:,}（{cache_rate:.1%}）",
            "同じモデル・同程度の作業と比較し、固定の指示や前提が毎回変わっていないか確認する。新規作業では低い比率でも正常。",
        )
    if growth is not None and growth >= 2 and last >= 20_000:
        hint(
            "input_growth",
            "後半の入力が増えている",
            f"先頭1/3の入力中央値 {first:,.0f} → 末尾1/3 {last:,.0f}（{growth:.1f}倍、{len(inputs)}記録）",
            "増加した記録付近の大きなツール出力を確認する。独立した作業は、目的・決定・検証結果を引き継いで会話を分ける方法を試す。",
        )
    if repeats >= 3:
        hint(
            "repeated_tools",
            "同じツール・引数の反復",
            f"同一引数の追加実行 {repeats} 回 / ツール呼出 {len(calls)} 回",
            "同じ調査をやり直していないか確認し、結果を引き継ぐ。監視・ポーリング・変更後の再検証なら必要な反復。",
        )
    if failures:
        hint(
            "tool_errors",
            "ツールのエラー・非ゼロ終了",
            f"明示的なエラーフラグまたは非ゼロ終了 {failures} 件 / 結果 {len(results)} 件（終了状態判定可能 {known} 件）",
            "該当するツール結果を元ログで確認し、依存関係や実行条件を先に整える。検索の該当なし等の非ゼロ終了は正常な場合もある。",
        )
    large = sorted(results.items(), key=lambda x: x[1]["output_chars"], reverse=True)
    if large and large[0][1]["output_chars"] >= 20_000:
        hint(
            "large_tool_output",
            "大きなツール出力",
            f"最大 {large[0][1]['output_chars']:,} 文字のツール結果（トークン数ではない）",
            "必要な範囲だけを読み、検索結果・テストログを絞る。入力増加との時刻の対応を確認する。",
        )
    if not ordered:
        status = "使用量の記録なし・判断不可"
    elif observations:
        status = "確認候補あり（非効率の断定ではない）"
    else:
        status = "設定した検出条件に該当なし（効率・品質は未判定）"

    hotspots = []
    recent_tools = sorted(calls.values(), key=lambda c: (c["timestamp"], c["line"]))
    for _, _, dt, model, token, source in sorted(ordered, key=lambda r: r[4]["input"] + r[4]["output"], reverse=True)[:10]:
        # Source line order is exact within the same file; timestamps order across files.
        before = [
            c
            for c in recent_tools
            if (c["source"] == source["source"] and c["line"] < source["line"])
            or (c["source"] != source["source"] and c["timestamp"] <= dt.astimezone(UTC).isoformat())
        ]
        hotspots.append(
            {
                "timestamp": dt.astimezone(tz).isoformat(),
                "model": model,
                **token,
                "total": token["input"] + token["output"],
                **source,
                "preceding_tools": [c["name"] for c in before[-3:]],
            }
        )
    evidence = []
    errors = [pair for pair in large if pair[1]["error"] is True][:10]
    selected = dict(errors + large[:10])
    for cid, result in selected.items():
        evidence.append(dict(result, tool=calls.get(cid, {}).get("name", "unknown")))
    repeated_groups = []
    for fingerprint, count in fingerprints.most_common(10):
        if count < 2:
            continue
        matches = sorted((c for c in calls.values() if c["fingerprint"] == fingerprint), key=lambda c: (c["timestamp"], c["line"]))
        first_call, last_call = matches[0], matches[-1]
        repeated_groups.append(
            {
                "tool": first_call["name"],
                "calls": count,
                "first_source": first_call["source"],
                "first_line": first_call["line"],
                "last_source": last_call["source"],
                "last_line": last_call["line"],
            }
        )
    stride = max(1, (len(ordered) + 58) // 59)
    indices = sorted(set(range(0, len(ordered), stride)) | ({len(ordered) - 1} if ordered else set()))
    timeline = [
        {
            "record": i + 1,
            "timestamp": ordered[i][2].astimezone(tz).isoformat(),
            "input": ordered[i][4]["input"],
            "cache_read": ordered[i][4]["cache_read"],
        }
        for i in indices
    ]
    session.update(
        {
            "total": total,
            "cache_read_rate": cache_rate,
            "cache_known": cache_known,
            "non_cached_input": fresh,
            "input_per_record": input_total / len(inputs) if inputs else None,
            "first_input_median": first,
            "last_input_median": last,
            "input_growth": growth,
            "tool_calls": len(calls),
            "repeated_tool_calls": repeats,
            "tool_results": len(results),
            "tool_errors": failures,
            "known_result_status": known,
            "compactions": compactions,
            "status": status,
            "hints": observations,
            "hotspots": hotspots,
            "tool_evidence": evidence,
            "models": sorted({r[3] for r in ordered}),
            "usage_formats": sorted({r[5]["format"] for r in ordered}),
            "outcome": "成果・完了・品質は未評価",
            "input_timeline": timeline,
            "repeated_tool_groups": repeated_groups,
        }
    )
    return session
