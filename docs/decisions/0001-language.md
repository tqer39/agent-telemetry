# 開発言語の選定

## 要件

Claude と Codex のローカル会話記録を読み、本文を外へ送らず集計値を Prometheus に公開する。
Python の集計パッケージと Docker Compose の監視構成を既存環境で継続して保守する。
テスト、静的解析、整形、パッケージのビルドを共通の mise タスクから実行する。

## 候補

Python は JSONL の解析と既存の prometheus-client、pytest、Ruff、uv の構成をそのまま利用できる。
Go は単体実行ファイルの配布に利点があるが、既存の集計処理の書き直しと再検証が必要になる。
TypeScript は周辺ツールが豊富だが、この用途で別の実行系へ移す要件はない。

## 選定理由

Python を継続採用する。既存の集計処理とテストを維持し、uv.lock を使って依存を再現する。
コンポーネントは `session-indexer` の１つとし、Docker Compose と既存の Lefthook は維持する。
dev-standards 本体が Go 製であることは、利用先の実装言語を変更する理由にはしない。
