"""Development checks must use the pinned runtime and fail on degraded validation."""

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/lint/validate_renovate.py"
spec = importlib.util.spec_from_file_location("validate_renovate", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@pytest.fixture
def pinned_runtime(tmp_path, monkeypatch):
    node = tmp_path / "node"
    npx = tmp_path / "npx-cli.js"
    node.touch()
    npx.touch()
    monkeypatch.setattr(validator.shutil, "which", lambda _: str(tmp_path / "mise"))
    monkeypatch.setattr(validator.subprocess, "check_output", lambda args, **_: str(node if args[-1] == "node" else npx))
    return node, npx


def test_uses_pinned_node_for_npx_and_child_processes(pinned_runtime, monkeypatch):
    node, npx = pinned_runtime
    monkeypatch.setenv("PATH", "/system-node-26/bin")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Config validated successfully\n", "")

    monkeypatch.setattr(validator.subprocess, "run", run)
    assert validator.main(["config with spaces.json5"]) == 0
    args, kwargs = calls[0]
    assert args[:2] == [str(node), str(npx)]
    assert args[-1] == "config with spaces.json5"
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(node.parent)
    assert not kwargs.get("shell", False)


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "expected"),
    [(2, "", "invalid config", 2), (0, "RE2 not usable, falling back to RegExp", "", 1), (0, "", "RE2 not usable", 1)],
)
def test_failed_or_degraded_validator_is_not_success(pinned_runtime, monkeypatch, code, stdout, stderr, expected):
    monkeypatch.setattr(validator.subprocess, "run", lambda args, **_: subprocess.CompletedProcess(args, code, stdout, stderr))
    assert validator.main(["renovate.json5"]) == expected


def test_missing_mise_is_failure(monkeypatch):
    monkeypatch.setattr(validator.shutil, "which", lambda _: None)
    assert validator.main([]) == 1


def test_missing_pinned_runtime_is_failure(pinned_runtime):
    pinned_runtime[0].unlink()
    assert validator.main([]) == 1


def test_renovate_manager_finds_the_runtime_version_pin():
    root = SCRIPT.parents[2]
    manager = json.loads((root / "renovate.json5").read_text())["customManagers"][0]
    pattern = manager["managerFilePatterns"][0][1:-1]
    assert re.fullmatch(pattern, str(SCRIPT.relative_to(root)).replace(os.sep, "/"))
    version_pattern = manager["matchStrings"][0].replace("(?<currentValue>", "(?P<currentValue>")
    match = re.search(version_pattern, SCRIPT.read_text())
    assert match is not None
    assert match.group("currentValue") == validator.RENOVATE_VERSION


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell fixture")
def test_client_check_uses_pinned_python_when_system_python_is_too_old(tmp_path):
    import shutil

    mise = shutil.which("mise")
    assert mise is not None
    fake_python = tmp_path / "python3"
    fake_python.write_text("#!/bin/sh\nexit 19\n")
    fake_python.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    result = subprocess.run(  # noqa: S603 -- fixed local task, no user input
        [mise, "run", "client:check"], cwd=SCRIPT.parents[2], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, "client:check should use the configured Python runtime"
    assert "Dry run only" in result.stdout
