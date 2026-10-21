"""USR-323: line_validate --help / unknown args must not snapshot or run the gate."""

from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def lv(monkeypatch):
    spec = importlib.util.spec_from_file_location("lv_usr323", REPO_ROOT / "scripts" / "line_validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[str] = []

    def boom(name):
        def _f(*a, **k):
            calls.append(name)
            return 0

        return _f

    monkeypatch.setattr(module, "build_snapshot_commit", boom("snapshot"))
    monkeypatch.setattr(module, "run_official_runner_on_snapshot", boom("snapshot_run"))
    monkeypatch.setattr(module, "run_steps_directly", boom("direct"))
    monkeypatch.setattr(module, "is_clean_tree", boom("status"))
    monkeypatch.setattr(module.subprocess, "call", boom("runner"))
    monkeypatch.setattr(module.subprocess, "run", boom("subprocess"))
    module.calls = calls
    return module


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero_without_side_effects(lv, flag, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        lv.main(REPO_ROOT, [flag])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
    assert lv.calls == []


def test_unknown_argument_exits_two_without_side_effects(lv) -> None:
    with pytest.raises(SystemExit) as exc:
        lv.main(REPO_ROOT, ["--bogus"])
    assert exc.value.code == 2
    assert lv.calls == []


@pytest.mark.parametrize(("args", "exit_code"), [(["--help"], 0), (["-h"], 0), (["--bogus"], 2)])
def test_entrypoint_parses_process_arguments_before_operations(
    lv, monkeypatch, capsys, args: list[str], exit_code: int
) -> None:
    operations: list[str] = []

    def record_run(*args, **kwargs):
        operations.append("subprocess.run")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    def record_call(*args, **kwargs):
        operations.append("subprocess.call")
        return 0

    monkeypatch.setattr(subprocess, "run", record_run)
    monkeypatch.setattr(subprocess, "call", record_call)
    monkeypatch.setattr(sys, "argv", [str(REPO_ROOT / "scripts" / "line_validate.py"), *args])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO_ROOT / "scripts" / "line_validate.py"), run_name="__main__")

    assert exc.value.code == exit_code
    assert operations == []
    output = capsys.readouterr()
    assert "usage" in (output.out + output.err).lower()


def test_clean_tree_passes_exact_quick_command_and_exit_code_through(lv, monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(lv, "is_clean_tree", lambda root: True)

    def runner_call(command, **kwargs):
        calls.append((list(command), kwargs))
        return 7

    monkeypatch.setattr(lv.subprocess, "call", runner_call)
    assert lv.main(REPO_ROOT, []) == 7

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        lv.sys.executable,
        str(REPO_ROOT / "core" / "harness" / "runner.py"),
        "--quick",
    ]
    assert kwargs["cwd"] == REPO_ROOT
    assert kwargs["env"] == lv.runner_env()


def test_dirty_tree_uses_snapshot_and_propagates_result(lv, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lv, "is_clean_tree", lambda root: False)

    def snapshot_run(root):
        calls.append("snapshot")
        return 5

    monkeypatch.setattr(lv, "run_official_runner_on_snapshot", snapshot_run)
    monkeypatch.setattr(lv, "run_steps_directly", lambda *args: calls.append("fallback") or 0)

    assert lv.main(REPO_ROOT, []) == 5
    assert calls == ["snapshot"]


def test_dirty_tree_snapshot_error_uses_fallback_and_propagates_result(lv, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lv, "is_clean_tree", lambda root: False)

    def snapshot_error(root):
        calls.append("snapshot")
        raise lv.SnapshotError("snapshot unavailable")

    def fallback(root, config_path):
        calls.append("fallback")
        assert config_path == root / "harness.config.json"
        return 6

    monkeypatch.setattr(lv, "run_official_runner_on_snapshot", snapshot_error)
    monkeypatch.setattr(lv, "run_steps_directly", fallback)

    assert lv.main(REPO_ROOT, []) == 6
    assert calls == ["snapshot", "fallback"]


def test_dirty_tree_nonzero_runner_result_does_not_fallback(lv, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lv, "is_clean_tree", lambda root: False)
    monkeypatch.setattr(lv, "run_official_runner_on_snapshot", lambda root: calls.append("snapshot") or 9)
    monkeypatch.setattr(lv, "run_steps_directly", lambda *args: calls.append("fallback") or 0)

    assert lv.main(REPO_ROOT, []) == 9
    assert calls == ["snapshot"]


@pytest.mark.parametrize("process_args", [["--help"], ["--bogus"], ["extra"]])
def test_main_without_explicit_argv_ignores_host_process_arguments(lv, monkeypatch, process_args) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lv.sys, "argv", ["line_validate.py", *process_args])
    monkeypatch.setattr(lv, "is_clean_tree", lambda root: True)
    monkeypatch.setattr(lv.subprocess, "call", lambda command, **kwargs: calls.append(command[-1]) or 3)

    assert lv.main(REPO_ROOT) == 3
    assert calls == ["--quick"]


def test_no_arguments_keeps_official_validation(lv, monkeypatch) -> None:
    lv.is_clean_tree = lambda root: True
    calls: list[list[str]] = []
    monkeypatch.setattr(lv.subprocess, "call", lambda command, **kwargs: calls.append(list(command)) or 0)
    assert lv.main(REPO_ROOT, []) == 0
    assert calls == [[lv.sys.executable, str(REPO_ROOT / "core" / "harness" / "runner.py"), "--quick"]]
