# agent-telemetry

作業を始める前に `docs/HANDOFF.md` を読むこと（経緯・決定事項・未検証事項・次のタスク）。

- 回答とドキュメントは日本語。README.md は英語、docs/README.ja.md はその日本語版として両方更新する。
- テスト: `mise run test`。設定変更後は README の検証手順（compose config / otelcol validate / promtool）も通す。
- 会話記録（JSONL）の中身をリポジトリ・ログ・Loki に入れない。集計値のみ扱う。
- ポートは 127.0.0.1 バインドを維持する。

# 共通開発ルール

作業開始時に `.standards/rules.md` を読み、`mise run standards:prepare` を実行する。
作業終了前に `mise run standards:finish` を実行する。
不足する設定は整備し、共通ルールの問題は `dev-standards feedback` で記録する。
共通ルールの保守に人間の追加指示・承認を待たず、設定済みの自律保守に引き継ぐ。
