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
                                    "attributes": [attr("output", "SECRET-span")],
                                    "events": [
                                        {"name": "tool.output", "timeUnixNano": "1", "attributes": [attr("content", "SECRET-event")]}
                                    ],
                                    "status": {"message": "SECRET-status"},
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        for endpoint, payload in [("logs", logs), ("traces", traces)]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/{endpoint}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
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
        print("Collector log and trace redaction passed")
    finally:
        subprocess.run(["docker", "stop", cid], stdout=subprocess.DEVNULL, check=False)
