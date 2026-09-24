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
import shutil
import subprocess
import sys
import tempfile
import threading
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


def write_python_shim(base: Path, script_path: Path) -> Path:
    """Executable shim running `script_path` with this interpreter.

    `.cmd` on Windows, a `#!/bin/sh` script elsewhere (CI runs Ubuntu too);
    both work as argv[0] without `shell=True`.
    """
    if sys.platform == "win32":
        shim = base.with_suffix(".cmd")
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")
    else:
        shim = base.with_suffix(".sh")
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    return shim


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
        cmd_path = write_python_shim(tmp_path / f"fake_{name}", script_path)
        created[name] = (str(cmd_path), env_var)
        return created[name]

    return _make


def set_fake_response(monkeypatch: pytest.MonkeyPatch, env_var: str, spec: dict) -> None:
    """Configure the next invocation of a fake CLI created by `make_fake_cli`."""
    monkeypatch.setenv(env_var, json.dumps(spec))


# ---------------------------------------------------------------------------
# Shared bare-origin git template (test-suite acceleration, rule C2)
#
# Dozens of tests across tests/line/ (and tests/test_line_e2e_hf2708.py)
# each build their own throwaway local "origin" remote via ~8 real git
# subprocess calls (init --bare, clone, checkout -B, config x2, add,
# commit, push). On Windows, each subprocess costs tens of milliseconds of
# pure process-spawn overhead (measured: `_winapi.CreateProcess` alone
# ~85ms/call) -- multiplied by dozens of call sites, that adds up to real
# seconds of the suite's wall time spent rebuilding byte-identical content.
# This builds ONE such repo per worker process (lazily, on first use) and
# lets callers get a fresh copy via a plain directory copy (no subprocess
# at all) instead.
# ---------------------------------------------------------------------------

_BARE_ORIGIN_TEMPLATE_LOCK = threading.Lock()
_BARE_ORIGIN_TEMPLATE: Path | None = None


def _template_git_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _template_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_template_git_kwargs(),
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"


def _build_bare_origin_template() -> Path:
    base = Path(tempfile.mkdtemp(prefix="darkfac-bare-origin-template-"))
    origin = base / "origin.git"
    _template_git(["init", "--bare", str(origin)], cwd=base)
    seed = base / "_seed"
    _template_git(["clone", str(origin), str(seed)], cwd=base)
    _template_git(["checkout", "-B", "main"], cwd=seed)
    _template_git(["config", "user.email", "seed@example.com"], cwd=seed)
    _template_git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _template_git(["add", "README.md"], cwd=seed)
    _template_git(["commit", "-m", "seed commit"], cwd=seed)
    _template_git(["push", "origin", "main"], cwd=seed)
    shutil.rmtree(seed, ignore_errors=True)
    return origin


def bare_origin_template() -> Path:
    """A bare "origin" repo (branch `main`, one `README.md` seed commit),
    built once per worker process and reused by `copy_bare_origin` --
    thread-safe so parallel (in-process-threaded) callers never race the
    one-time build.
    """
    global _BARE_ORIGIN_TEMPLATE
    with _BARE_ORIGIN_TEMPLATE_LOCK:
        if _BARE_ORIGIN_TEMPLATE is None:
            _BARE_ORIGIN_TEMPLATE = _build_bare_origin_template()
        return _BARE_ORIGIN_TEMPLATE


def copy_bare_origin(dest: Path) -> Path:
    """Fast per-test "origin" remote: a plain directory copy of the shared
    template instead of the ~8 git subprocess calls that originally built
    it. Safe to call concurrently across tests/workers -- copytree only
    reads the (immutable, already-fully-built) template.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bare_origin_template(), dest)
    return dest
