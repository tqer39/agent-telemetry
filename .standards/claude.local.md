# agent-telemetry

作業を始める前に `docs/HANDOFF.md` を読むこと（経緯・決定事項・未検証事項・次のタスク）。

- 回答とドキュメントは日本語。README.md は英語、docs/README.ja.md はその日本語版として両方更新する。
- テスト: `mise run test`。設定変更後は README の検証手順（compose config / otelcol validate / promtool）も通す。
- 会話記録（JSONL）の中身をリポジトリ・ログ・Loki に入れない。集計値のみ扱う。
- ポートは 127.0.0.1 バインドを維持する。
