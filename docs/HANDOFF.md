# 引き継ぎメモ（2026-10-29 Chat セッションより）

## 目的
Claude（Desktop / Code）と Codex（Desktop / CLI）による自律開発が「遅い・どこで詰まっているか分からない」問題を、**計測で可視化**する。手法改善（LLM 同士のレビューループ等）は計測が取れてから判断する、という合意で進めている。

## 前提として決まったこと
- 最適化の主指標は「手戻り（実行内ループ含む）」。見る指標: 実稼働時間 / トークン・キャッシュ率 / ツール呼び出し数・失敗数 / 同一コマンド反復 / 失敗後の再実行 / PR がマージされたか。
- Claude・Codex ともに **Desktop アプリが主**。シェル変数は効かないので設定は `~/.claude/settings.json` の `env` と `~/.codex/config.toml` の `[otel]` に書く。
- LLM 同士のフィードバックループは「差分レビュー用途のみ・最大 2 往復・blocking のみ差し戻し・意見が割れたら人間」。要件の議論を LLM 同士にさせない。
- 統合ネイティブアプリは**まだ作らない**。2 週間 Grafana で運用し、足りない画面を確定してから。

## 現状（このリポジトリ）
- Mizune/llm-cli-telemetry（MIT, commit 6d224e8）ベース。ジェネレータスクリプトは廃止し静的設定に。
- 追加: `session-indexer`（Python/uv）が `~/.claude/projects` と `~/.codex/sessions` の JSONL を**集計のみ**して Prometheus に公開。生ログは Loki に送らない（コード・シークレット流出防止）。
- 追加: Agent Sessions ダッシュボード、`scripts/apply-client-config.py`（バックアップ付き・冪等）。
- セキュリティ: 全ポート 127.0.0.1、Grafana 初期パスワード廃止、indexer は read-only / 非 root / cap_drop ALL。
- terraform-github 用の定義は `terraform-github-patch/` に別途ある（private, media-server と同じ形式）。

## 検証済み / 未検証
- 済: pytest 9 件、ruff、compose config、otelcol validate、promtool、yamllint、actionlint、実 Prometheus 上でダッシュボード全クエリが値を返すこと（フィクスチャ使用）。
- 済（2026-10-29）: 実データでのパーサー検証（直近 7 日、Claude 6 / Codex 22 セッション）。修正点は下記。
- **未: Docker 上での起動**、Desktop アプリから実際に届く OTLP シグナルの種類。

### 実データ検証で分かったこと（2026-10-29）
- Codex Desktop はシェルを `custom_tool_call name=exec`（中身はコード）で包み、実際の実行は `event_msg` / `item_completed` の `item.type = CommandExecution`（`command` 配列・`exit_code`・`status`）に出る。旧来の `function_call name=shell` 経路では何も取れていなかったので、item ベースの集計を追加。
- Codex Desktop はサブエージェントごとに別ファイル（`session_meta.parent_thread_id` あり）を作り、中身が compaction だけの空ファイルも多い。親へ合算し、空ファイルは除外（81 → 28 セッション）。トークンはサブエージェントごとに 0 から積算されるので合算で正しい。
- Codex の `total_token_usage` に `cache_write_input_tokens` が増えた（現状すべて 0）。input に含まれる前提で分離。
- シェルラッパーに `/opt/homebrew/bin/bash -c` がある。basename で判定するよう修正。
- Claude の cwd は `<repo>/.claude/worktrees/<名前>` になることが多い。`<repo>` に寄せる。
- Claude のトランスクリプトには `api-request-blob` 行に tool_use の複製が入る。パーサーは `assistant` 行だけを見るので二重計上はない（反復 0 は実データ通り）。

## 次にやること（優先順）
1. ~~`mise run report` と `session-indexer schema <実ファイル>` で実データとのズレを確認し、パーサーを修正~~（2026-10-29 済）
2. `mise run up` → `DEBUG_VERBOSITY=detailed` で Desktop 2 種から何が届くか記録し README に追記。
3. PR マージ状況コレクター（GitHub API, read-only fine-grained token）。
4. Claude → Codex の `run_id` 伝播（Claude 側 skill と Codex 側 AGENTS.md）。

## 注意
- フックは pre-commit ではなく **lefthook**（terraform-github が lefthook 移行済みで renovate.json5 も前提にしているため）。ユーザーの開発ルール記載とは異なるので、確認が取れるまで変更しないこと。
- ベース OSS の `scripts/install.sh` は Codex 用に**不正な TOML**を生成する（`metrics_exporter` を文字列とテーブルで二重定義）。流用しないこと。
- Codex の `[otel]` キーはバージョンで変わる。動かなければ openai/codex の docs/config.md と突き合わせる。
