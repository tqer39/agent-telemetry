"""Normalize usage metadata; never retain message content in report data."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .efficiency import Trace, digest, repository, summarize
from .logs import READ_ERRORS, ZstdUnavailableError, candidates, logical_path, open_log

FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


def number(value):
    return value if type(value) is int and value >= 0 else 0


def stamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def label(value, default):
    return value if isinstance(value, str) and value else default


def tokens(usage, provider):
    u = mapping(usage)
    read = number(u.get("cache_read_input_tokens" if provider == "claude" else "cached_input_tokens"))
    write = number(u.get("cache_creation_input_tokens" if provider == "claude" else "cache_write_input_tokens"))
    return dict(
        zip(
            FIELDS,
            (
                number(u.get("input_tokens")) + (read + write if provider == "claude" else 0),
                number(u.get("output_tokens")),
                read,
                write,
                number(u.get("reasoning_output_tokens")),
            ),
            strict=True,
        )
    )


def cache_known(usage, provider, counts):
    key = "cache_read_input_tokens" if provider == "claude" else "cached_input_tokens"
    return type(usage.get(key)) is int and usage[key] >= 0 and counts["cache_read"] <= counts["input"]


def read_session(path, provider, diagnostics, in_range):
    """Stage metadata until the entire compressed stream has been validated."""
    identity = str(logical_path(path))
    sid = identity
    model = "unknown"
    last_activity = None
    modern = []
    legacy = []
    high = dict.fromkeys(FIELDS, 0)
    trace = Trace(path, provider, in_range)
    with open_log(path) as stream:
        for index, line in enumerate(stream):
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                diagnostics["invalid_lines"] += 1
                continue
            if not isinstance(row, dict):
                diagnostics["invalid_lines"] += 1
                continue
            dt = stamp(row.get("timestamp"))
            trace.observe(row, dt, index + 1)
            if dt and in_range(dt) and (last_activity is None or dt > last_activity):
                last_activity = dt
            kind = row.get("type")
            p = mapping(row.get("payload"))
            if provider == "claude":
                m = mapping(row.get("message"))
                sid = label(row.get("sessionId"), sid)
                if kind == "assistant" and isinstance(m.get("usage"), dict):
                    key = (provider, label(m.get("id"), label(row.get("uuid"), f"{identity}:{index}")))
                    counts = tokens(m["usage"], provider)
                    evidence = {
                        "source": str(path),
                        "line": index + 1,
                        "format": "claude_response",
                        "cache_known": cache_known(m["usage"], provider, counts),
                    }
                    modern.append((key, provider, sid, dt, label(m.get("model"), "unknown"), counts, evidence))
            elif kind == "session_meta":
                sid = label(p.get("id"), label(p.get("session_id"), sid))
            elif kind == "turn_context":
                model = label(p.get("model"), "unknown")
            elif kind == "token_usage_record" and isinstance(p.get("usage"), dict):
                key = (provider, sid, label(p.get("response_id"), f"{identity}:{index}"))
                counts = tokens(p["usage"], provider)
                evidence = {
                    "source": str(path),
                    "line": index + 1,
                    "format": "codex_response",
                    "cache_known": cache_known(p["usage"], provider, counts),
                }
                modern.append((key, provider, sid, dt, model, counts, evidence))
            elif kind == "event_msg" and p.get("type") == "token_count":
                total = mapping(p.get("info")).get("total_token_usage")
                if isinstance(total, dict):
                    current = tokens(total, provider)
                    delta = {k: max(0, current[k] - high[k]) for k in FIELDS}
                    high = {k: max(high[k], current[k]) for k in FIELDS}
                    if delta["input"] or delta["output"]:
                        key = (provider, sid, "cumulative", tuple(high.values()))
                        evidence = {
                            "source": str(path),
                            "line": index + 1,
                            "format": "codex_cumulative_delta",
                            "cache_known": cache_known(total, provider, delta),
                        }
                        legacy.append((key, provider, sid, dt, model, delta, evidence))
    return sid, last_activity, modern or legacy, trace.export()


def scan(claude_root, codex_root, days=30, tz=None, now=None):
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    start = today - timedelta(days=days - 1) if days else None
    diagnostics = defaultdict(int)
    sessions = {}
    requests = {}
    sources = []
    traces = {}

    def in_range(dt):
        day = dt.astimezone(tz).date()
        return (start is None or day >= start) and day <= today

    def keep_request(key, provider, session, dt, model, counts, evidence):
        if not dt:
            diagnostics["usage_without_timestamp"] += 1
            return
        if not in_range(dt):
            return
        old = requests.get(key)
        if old:
            # Claude streaming blocks can repeat the same response with updated usage.
            incoming = counts
            counts = {k: max(old[4][k], counts[k]) for k in FIELDS}
            dt = min(dt, old[2])
            # Trace the more complete snapshot, even if an older copy is read later.
            if old[4]["input"] + old[4]["output"] > incoming["input"] + incoming["output"]:
                evidence = dict(old[5])
        requests[key] = (provider, session, dt, model, counts, evidence)

    for provider, roots in (
        ("claude", [Path(claude_root)]),
        ("codex", [Path(codex_root) / "sessions", Path(codex_root) / "archived_sessions"]),
    ):
        found = compressed = read = 0
        for root in roots:
            for alternatives in candidates(root, diagnostics):
                found += 1
                for index, path in enumerate(alternatives):
                    try:
                        sid, last_activity, records, trace = read_session(path, provider, diagnostics, in_range)
                    except ZstdUnavailableError:
                        diagnostics["zstd_unavailable_files"] += 1
                        continue
                    except READ_ERRORS:
                        diagnostics["corrupt_compressed_files" if path.suffix != ".jsonl" else "unreadable_files"] += 1
                        continue
                    for record in records:
                        keep_request(*record)
                    if last_activity:
                        key = (provider, sid)
                        sessions[key] = max(sessions.get(key, last_activity), last_activity)
                        existing = traces.setdefault(
                            key,
                            {
                                "cwd": trace["cwd"],
                                "recorded_remote": trace["recorded_remote"],
                                "calls": {},
                                "results": {},
                                "events": {},
                                "sources": [],
                            },
                        )
                        if existing["cwd"] is None:
                            existing["cwd"] = trace["cwd"]
                        if existing["recorded_remote"] is None:
                            existing["recorded_remote"] = trace["recorded_remote"]
                        for field in ("calls", "results", "events"):
                            existing[field].update(trace[field])
                        existing["sources"].append(str(path))
                    read += 1
                    compressed += path.suffix != ".jsonl"
                    skipped = len(alternatives) - index - 1
                    if skipped:
                        diagnostics["duplicate_files"] += skipped
                    break
        sources.append({"provider": provider, "found": bool(found), "files": found, "read_files": read, "compressed_files": compressed})

    def bucket():
        return dict.fromkeys((*FIELDS, "requests"), 0)

    providers = {p: dict(bucket(), sessions=0, sessions_without_usage=0, last_activity=None) for p in ("claude", "codex")}
    daily = defaultdict(bucket)
    models = defaultdict(bucket)
    used_sessions = set()
    session_records = defaultdict(list)
    session_totals = defaultdict(bucket)
    for record in requests.values():
        provider, sid, dt, model, counts, evidence = record
        if not in_range(dt):
            continue
        used_sessions.add((provider, sid))
        day = dt.astimezone(tz).date().isoformat()
        session_records[(provider, sid)].append(record)
        for target in (providers[provider], daily[(day, provider)], models[(provider, model)], session_totals[(provider, sid)]):
            target["requests"] += 1
            for field in FIELDS:
                target[field] += counts[field]
    for key, activity in sessions.items():
        target = providers[key[0]]
        target["sessions"] += 1
        target["sessions_without_usage"] += key not in used_sessions
        last = activity.astimezone(tz).isoformat()
        if target["last_activity"] is None or stamp(last) > stamp(target["last_activity"]):
            target["last_activity"] = last
    session_rows = []
    for key, activity in sessions.items():
        provider, sid = key
        trace = traces[key]
        repo = repository(trace["cwd"], trace["recorded_remote"])
        row = dict(
            session_totals[key],
            provider=provider,
            session_id=sid,
            anchor="session-" + digest(key),
            cwd=trace["cwd"],
            repository=repo["label"],
            repository_key=repo["key"],
            repository_basis=repo["basis"],
            sources=trace["sources"],
            last_activity=activity.astimezone(tz).isoformat(),
        )
        session_rows.append(summarize(row, session_records[key], trace, tz))
    session_rows.sort(key=lambda s: s["total"], reverse=True)
    repos = {}
    for row in session_rows:
        group = repos.setdefault(
            row["repository_key"],
            dict(
                bucket(),
                repository=row["repository"],
                repository_key=row["repository_key"],
                repository_basis=row["repository_basis"],
                sessions=0,
                hints=0,
            ),
        )
        if group["repository_basis"] != row["repository_basis"]:
            group["repository_basis"] = "ログ記録・現在のGit情報（各詳細を参照）"
        group["sessions"] += 1
        group["hints"] += len(row["hints"])
        for field in (*FIELDS, "requests"):
            group[field] += row[field]
    return {
        "generated_at": now.isoformat(),
        "days": days,
        "timezone": str(tz) if tz else "system local",
        "start_date": start.isoformat() if start else None,
        "end_date": today.isoformat(),
        "providers": providers,
        "sources": sources,
        "diagnostics": dict(diagnostics),
        "daily": [dict(date=d, provider=p, **v) for (d, p), v in sorted(daily.items(), reverse=True)],
        "models": [dict(provider=p, model=m, **v) for (p, m), v in sorted(models.items())],
        "sessions": session_rows,
        "repositories": sorted(repos.values(), key=lambda r: r["input"] + r["output"], reverse=True),
    }
