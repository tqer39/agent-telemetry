FROM busybox:1.38.0-musl AS probe
FROM grafana/tempo:2.10.10
# distroless の実行イメージでも HTTP readiness を検査するために追加する。
COPY --from=probe /bin/busybox /usr/local/bin/wget
