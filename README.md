# agent-telemetry

Local observability for **Claude Code / Claude Desktop** and **Codex CLI / Codex Desktop** in one Grafana.

Two independent data paths, so a gap in one is covered by the other:

| Path | Source | What you get | Works for Desktop apps |
|---|---|---|---|
| OTLP | the apps' built-in OpenTelemetry export | cost, token, API latency, tool events, traces | Yes, via `~/.claude/settings.json` and `~/.codex/config.toml` (Desktop apps never read `~/.zshrc`) |
| Transcripts | `~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/*.jsonl` | per-session duration, tokens, cache hit ratio, tool calls / failures, repeated commands, retries after failure | Yes, both apps write these files |

```text
Claude Desktop / Code ─┐ OTLP                      ┌─ Prometheus ─┐
Codex  Desktop / CLI ──┴─> otel-collector ─────────┼─ Loki ───────┼─> Grafana :3000
~/.claude/projects ─┐                              └─ Tempo ──────┘
~/.codex/sessions ──┴─> session-indexer :9464 ──> Prometheus (scrape)
```

Transcripts are **summarized, never shipped**: source code, prompts and secrets in them stay on disk.

## Quick start

```bash
mise run setup
cp .env.example .env            # set GF_SECURITY_ADMIN_PASSWORD
mise run up                     # docker compose up -d --build
mise run client:check           # dry run of the Desktop/CLI settings change
mise run client:apply           # write them (backups are created)
# fully quit and reopen Claude Desktop and Codex Desktop
open http://127.0.0.1:3000      # dashboards in the "Agent Telemetry" folder
```

No Docker? `mise run report` prints the per-session table straight from the transcripts.

## Verify what actually arrives (do this once)

Desktop apps may send fewer signals than the CLIs. Do not assume; check:

1. Set `DEBUG_VERBOSITY=detailed` in `.env`, `docker compose up -d otel-collector`.
2. `mise run verify`, then run one small task in each Desktop app.
3. Note which of metrics / logs / traces appear per app; set verbosity back to `basic` and run `docker compose up -d otel-collector` again (the running container keeps the old value).

Anything missing on the OTLP path is still covered by the **Agent Sessions** dashboard (transcript path).

## Dashboards

| Dashboard | Source |
|---|---|
| Agent Sessions (Claude + Codex) | session-indexer (new) |
| Claude Code / Codex CLI / Comparison / Overview | OTLP (from llm-cli-telemetry) |

## Metrics from session-indexer

All gauges carry `tool`, `session_id`, `project` (`project` = repo name from Codex git metadata, else the cwd basename; Claude worktrees under `<repo>/.claude/worktrees/` map to `<repo>`).

`agent_session_{start_time_seconds,wall_seconds,active_seconds,tokens{type},cache_hit_ratio,tool_calls,tool_failures,shell_commands,repeated_commands,retries_after_failure,parse_errors}`, `agent_session_info{model}`, `agent_indexer_*`.

- **active_seconds**: sum of gaps between events, ignoring gaps longer than `IDLE_GAP_SECONDS` (default 300). Wall time includes idle time and is misleading for interactive sessions.
- **retries_after_failure**: a shell command re-run while its previous run had failed. The main "stuck in a loop" signal.
- Codex `input_tokens` excludes cached tokens (OpenAI reports cached inside input; it is split out here so the ratio is comparable with Claude).
- Codex subagent threads (`parent_thread_id`) are merged into their parent session, like Claude subagents. Empty subagent stubs are skipped.
- Codex Desktop runs tools through `exec`/`js` wrappers; `tool_calls` counts the executed items (commands, MCP calls, file changes) reported as `item_completed`, not the wrappers.

The transcript formats are undocumented. If numbers look wrong after an app update, inspect the structure without exposing content:

```bash
uv run --project session-indexer session-indexer schema ~/.codex/sessions/2026/10/29/rollout-....jsonl
```

## Not included (yet)

- **PR merged or not**: needs the GitHub API and a session to PR link. Planned as a separate collector.
- **Claude to Codex correlation**: needs a shared `run_id` passed in the prompt/context. Planned.

## Security

- Every port is bound to `127.0.0.1`. Grafana has no default password and anonymous access is off.
- Transcript dirs are mounted read-only into a read-only, non-root, capability-free container.
- `log_user_prompt = false` for Codex; Claude Code redacts prompts unless `OTEL_LOG_USER_PROMPTS=1`.

## Credits

Based on [Mizune/llm-cli-telemetry](https://github.com/Mizune/llm-cli-telemetry) (MIT, commit `6d224e8`): Loki/Tempo configs, datasources, recording rule and four dashboards. See `docs/THIRD_PARTY_LICENSE-llm-cli-telemetry`.

Japanese: [docs/README.ja.md](docs/README.ja.md)

## Development standards

This repository uses the `python-uv` profile of dev-standards for `session-indexer`.
Run `mise run standards:prepare` before working and `mise run standards:verify`
followed by `mise run standards:finish` before finishing. `mise run check` runs the
configuration checks, existing Lefthook checks, Ruff, pytest, and package build.
The previous combined test task is available as `mise run indexer:test`.

Edit project settings in `.standards/mise.project.toml`,
`.standards/tasks.project.toml`, and the other `.standards/*.local` sources.
Save and review a plan with
`./.standards/cache/dev-standards plan --output .standards/plans/update.json`,
then apply it with `./.standards/cache/dev-standards apply --plan .standards/plans/update.json`.
`docs/decisions/0001-language.md` records why Python is retained.
Existing Compose operations and client settings tasks keep their names.
The original CI workflow remains alongside the generated Dev Standards workflow.

The local maintenance fleet is registered; scheduled maintenance is not configured
by this onboarding. The Python profile changes in dev-standards and this adoption
must be committed and integrated before updates can be distributed from GitHub.

The Renovate hook invokes mise's pinned Node/npx directly, so a different system
Node cannot load native RE2 modules from the wrong ABI. RE2 fallback is a failed
check. Its pinned Renovate version remains tracked by the custom manager in
`renovate.json5`.

On a new checkout, build/install the matching dev-standards CLI from its Python/uv
support change, then run `dev-standards sync` to populate the ignored CLI cache
before using the standards tasks. Source changes must be integrated first.
