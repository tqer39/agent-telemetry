#!/usr/bin/env python3
# ruff: noqa: S101, S310, S603, S607, E731
"""Exercise the real collector with synthetic log and trace secrets."""

import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent.parent
config = (root / "otel-collector/config.yaml").read_text()
pipelines = config.split("  pipelines:\n", 1)[1]
for pipeline in ("traces", "logs"):
    section = pipelines.split(f"    {pipeline}:\n", 1)[1].split("      exporters:", 1)[0]
    assert "processors: [transform/redact," in section
redactor = config[config.index("  transform/redact:") : config.index("\nexporters:")]
test_config = (
    "receivers:\n  otlp:\n    protocols:\n      http:\n        endpoint: 0.0.0.0:4318\n"
    "processors:\n" + redactor + "\nexporters:\n  debug:\n    verbosity: detailed\n"
    "service:\n  pipelines:\n    logs:\n      receivers: [otlp]\n"
    "      processors: [transform/redact]\n      exporters: [debug]\n"
    "    traces:\n      receivers: [otlp]\n      processors: [transform/redact]\n      exporters: [debug]\n"
)

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "config.yaml"
    path.write_text(test_config)
    cid = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            "127.0.0.1::4318",
            "-v",
            f"{path}:/cfg.yaml:ro",
            "otel/opentelemetry-collector-contrib:0.120.0",
            "--config=/cfg.yaml",
        ],
        text=True,
    ).strip()
    try:
        port = subprocess.check_output(["docker", "port", cid, "4318/tcp"], text=True).strip().rsplit(":", 1)[1]
        attr = lambda key, value: {"key": key, "value": {"stringValue": value}}
        numeric = lambda key, value: {"key": key, "value": {"intValue": str(value)}}
        logs = {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "body": {"stringValue": "SECRET-body"},
                                    "attributes": [
                                        attr("conversation.id", "conversation-test"),
                                        attr("event.name", "skill_activated"),
                                        attr("output", "SECRET-log"),
                                        attr("unknown", "SECRET-unknown"),
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        traces = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "01" * 16,
                                    "spanId": "02" * 8,
                                    "name": "safe-span",
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                    "attributes": [
                                        attr("output", "SECRET-span"),
                                        numeric("input_tokens", 101),
                                        numeric("output_tokens", 202),
                                        numeric("cache_read_tokens", 303),
                                        numeric("cache_creation_tokens", 404),
                                        numeric("status_code", 429),
                                        numeric("attempt", 2),
                                    ],
                                    "events": [
                                        {
                                            "name": "tool.output",
                                            "timeUnixNano": "1",
                                            "attributes": [attr("content", "SECRET-event")],
                                        },
                                        {
                                            "name": "gen_ai.request.attempt",
                                            "timeUnixNano": "1",
                                            "attributes": [
                                                numeric("attempt", 3),
                                                attr("client_request_id", "safe-retry-id"),
                                                attr("content", "SECRET-retry"),
                                            ],
                                        },
                                    ],
                                    "status": {"message": "SECRET-status"},
                                },
                                {
                                    "traceId": "03" * 16,
                                    "spanId": "04" * 8,
                                    "name": "invalid-counter",
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                    "attributes": [attr("input_tokens", "SECRET-counter")],
                                    "events": [
                                        {
                                            "name": "gen_ai.request.attempt",
                                            "timeUnixNano": "1",
                                            "attributes": [attr("attempt", "SECRET-attempt")],
                                        }
                                    ],
                                },
                            ]
                        }
                    ]
                }
            ]
        }
        for endpoint, payload in [("logs", logs), ("traces", traces)]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/{endpoint}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            for attempt in range(30):
                try:
                    with urllib.request.urlopen(req, timeout=2) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if attempt == 29:
                        raise
                    time.sleep(0.2)
        time.sleep(1)
        result = subprocess.check_output(["docker", "logs", cid], stderr=subprocess.STDOUT, text=True)
        assert "SECRET-" not in result
        assert "conversation-test" in result and "skill_activated" in result and "safe-span" in result
        for key, value in [
            ("input_tokens", 101),
            ("output_tokens", 202),
            ("cache_read_tokens", 303),
            ("cache_creation_tokens", 404),
            ("status_code", 429),
            ("attempt", 2),
        ]:
            assert f"{key}: Int({value})" in result, key
        assert "safe-retry-id" in result and "attempt: Int(3)" in result
        print("Collector redaction and safe trace diagnostics passed")
    finally:
        subprocess.run(["docker", "stop", cid], stdout=subprocess.DEVNULL, check=False)
