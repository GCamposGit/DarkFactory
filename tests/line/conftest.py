"""Fake CLI executable factory for core.line tests.

Builds a tiny `.cmd` shim (Windows-friendly, works without shell=True as
long as an absolute path is used) that wraps a Python script. The script's
behaviour is controlled per-invocation through an environment variable
holding a JSON spec, so a single fake binary can be reused across many
scenarios (success, non-zero exit, rate limit text, etc.) without touching
the real Claude/Codex/Grok/Antigravity CLIs or the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import pytest

_FAKE_CLI_SCRIPT = '''\
import json, os, sys


def main():
    spec = json.loads(os.environ.get({env_var!r}, "{{}}"))
    argv = sys.argv[1:]
    try:
        stdin_data = sys.stdin.read()
    except Exception:
        stdin_data = ""

    record_path = spec.get("record_path")
    if record_path:
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump({{"argv": argv, "cwd": os.getcwd(), "stdin": stdin_data}}, fh)

    tmp_out_content = spec.get("tmp_out_content")
    if tmp_out_content is not None and "-o" in argv:
        idx = argv.index("-o")
        if idx + 1 < len(argv):
            with open(argv[idx + 1], "w", encoding="utf-8") as fh:
                fh.write(tmp_out_content)

    stdout = spec.get("stdout", "")
    stderr = spec.get("stderr", "")
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    sys.exit(int(spec.get("returncode", 0)))


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def make_fake_cli(tmp_path: Path) -> Callable[[str], tuple[str, str]]:
    """Return a factory `(name) -> (cmd_path, env_var_name)` for a fake CLI binary."""

    created: dict[str, tuple[str, str]] = {}

    def _make(name: str) -> tuple[str, str]:
        if name in created:
            return created[name]
        env_var = f"FAKE_CLI_RESPONSE_{name.upper()}"
        script_path = tmp_path / f"fake_{name}.py"
        script_path.write_text(_FAKE_CLI_SCRIPT.format(env_var=env_var), encoding="utf-8")
        cmd_path = tmp_path / f"fake_{name}.cmd"
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
            encoding="utf-8",
        )
        created[name] = (str(cmd_path), env_var)
        return created[name]

    return _make


def set_fake_response(monkeypatch: pytest.MonkeyPatch, env_var: str, spec: dict) -> None:
    """Configure the next invocation of a fake CLI created by `make_fake_cli`."""
    monkeypatch.setenv(env_var, json.dumps(spec))
