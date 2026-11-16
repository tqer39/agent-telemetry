#!/usr/bin/env bash
# 実行中のスタックやデータを共有せず、readiness と失敗時の終了コードを検査する。
set -euo pipefail
cd "$(dirname "$0")/.."
GF_SECURITY_ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
export GF_SECURITY_ADMIN_PASSWORD
scratch=$(mktemp -d)
compose=(docker compose -p "agent-telemetry-healthcheck-$$" -f compose.yaml -f "$scratch/ports.yaml")
cleanup() {
  result=$?
  if [ "$result" -ne 0 ]; then
    "${compose[@]}" logs --tail 50 loki tempo || true
  fi
  "${compose[@]}" down --volumes --rmi local --remove-orphans || true
  rm -rf "$scratch"
  exit "$result"
}
trap cleanup EXIT
cat > "$scratch/ports.yaml" <<'YAML'
services:
  loki:
    ports: !reset []
  tempo:
    ports: !reset []
YAML
"${compose[@]}" up -d --build --wait --wait-timeout 180 loki tempo
for service in loki tempo; do
  if [ "$service" = loki ]; then port=3100; else port=3200; fi
  "${compose[@]}" exec -T "$service" /usr/local/bin/wget --spider -q "http://localhost:$port/ready"
  if "${compose[@]}" exec -T "$service" /usr/local/bin/wget --spider -q "http://localhost:$port/does-not-exist"; then
    echo "$service: HTTP エラーが成功として扱われました" >&2
    exit 1
  fi
done
