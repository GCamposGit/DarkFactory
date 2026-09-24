"""Unit tests for the stateless test-impact selector (core/harness/affected.py).

Every scenario builds a minimal synthetic git repository under ``tmp_path``
that mirrors the real repo's scanned layout (``core/``, ``hub/``, ``tests/``)
so the selector's hardcoded scan roots apply. Tests call
:func:`core.harness.affected.select_affected` directly against that
synthetic repo -- no dependency on the real DarkFac test suite except for
the dedicated smoke test at the bottom.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.harness import affected


# --- fixture-repo helpers ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _init_repo(repo: Path, branch: str = "main") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "affected-tests@example.com")
    _git(repo, "config", "user.name", "Affected Tests")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# --- direct / transitive / relative imports ----------------------------------


def test_direct_and_transitive_import(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/leaf.py", "VALUE = 1\n")
    _write(repo, "core/mid.py", "import core.leaf\n\n\ndef read() -> int:\n    return core.leaf.VALUE\n")
    _write(
        repo,
        "tests/test_direct.py",
        "import core.leaf\n\n\ndef test_direct() -> None:\n    assert core.leaf.VALUE == 1\n",
    )
    _write(
        repo,
        "tests/test_transitive.py",
        "import core.mid\n\n\ndef test_transitive() -> None:\n    assert core.mid.read() == 1\n",
    )
    for index in range(4):
        _write(
            repo,
            f"tests/test_unrelated_{index}.py",
            f"def test_unrelated_{index}() -> None:\n    assert True\n",
        )
    _commit_all(repo, "baseline")

    _write(repo, "core/leaf.py", "VALUE = 2\n")  # uncommitted working-tree change

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_direct.py" in selection.tests
    assert "tests/test_transitive.py" in selection.tests
    for index in range(4):
        assert f"tests/test_unrelated_{index}.py" not in selection.tests
    assert "core/leaf.py" in selection.changed_files


def test_relative_import(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/pkg/__init__.py", "")
    _write(repo, "core/pkg/leaf.py", "VALUE = 1\n")
    _write(repo, "core/pkg/mid.py", "from . import leaf\n\n\ndef read() -> int:\n    return leaf.VALUE\n")
    _write(
        repo,
        "tests/test_relpkg.py",
        "import core.pkg.mid\n\n\ndef test_relpkg() -> None:\n    assert core.pkg.mid.read() == 1\n",
    )
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/pkg/leaf.py", "VALUE = 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_relpkg.py" in selection.tests
    assert "tests/test_unrelated.py" not in selection.tests


def test_from_pkg_import_submodule(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/pkg2/__init__.py", "")
    _write(repo, "core/pkg2/sub.py", "VALUE = 1\n")
    _write(
        repo,
        "core/pkg2_user.py",
        "from core.pkg2 import sub\n\n\ndef read() -> int:\n    return sub.VALUE\n",
    )
    _write(
        repo,
        "tests/test_pkg2_user.py",
        "import core.pkg2_user\n\n\ndef test_pkg2_user() -> None:\n    assert core.pkg2_user.read() == 1\n",
    )
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/pkg2/sub.py", "VALUE = 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_pkg2_user.py" in selection.tests
    assert "tests/test_unrelated.py" not in selection.tests


# --- literal text / subprocess reference -------------------------------------


def test_string_subprocess_reference(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/toolx/cli.py", "def main() -> int:\n    return 0\n")
    _write(
        repo,
        "tests/test_cli_invocation.py",
        "import subprocess\n\n\n"
        "def test_cli_invocation() -> None:\n"
        "    subprocess.run(['python', 'core/toolx/cli.py'])\n",
    )
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/toolx/cli.py", "def main() -> int:\n    return 1\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_cli_invocation.py" in selection.tests
    assert "text_reference: core/toolx/cli.py" in selection.reasons["tests/test_cli_invocation.py"]
    assert "tests/test_unrelated.py" not in selection.tests


# --- changed test file / conftest rules --------------------------------------


def test_changed_test_file_always_selected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_alone.py", "def test_alone() -> None:\n    assert True\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_alone.py", "def test_alone() -> None:\n    assert 1 == 1\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_alone.py" in selection.tests
    assert "tests/test_unrelated.py" not in selection.tests
    assert "changed_test_file" in selection.reasons["tests/test_alone.py"]


def test_root_conftest_escalates_to_full(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/conftest.py", "import pytest  # noqa: F401\n")
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert True\n")
    _write(repo, "tests/test_b.py", "def test_b() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/conftest.py", "import pytest  # noqa: F401\n# changed\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "full"
    assert any("tests/conftest.py" in escalation for escalation in selection.escalations)
    assert "tests/test_a.py" in selection.tests
    assert "tests/test_b.py" in selection.tests


def test_nested_conftest_selects_only_its_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/group/conftest.py", "import pytest  # noqa: F401\n")
    _write(repo, "tests/group/test_in_group.py", "def test_in_group() -> None:\n    assert True\n")
    _write(repo, "tests/test_outside.py", "def test_outside() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/group/conftest.py", "import pytest  # noqa: F401\n# changed\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/group/test_in_group.py" in selection.tests
    assert "tests/test_outside.py" not in selection.tests


# --- non-Python assets --------------------------------------------------------


def test_non_python_asset_matched_is_subset(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "hub/config/feature_flags.json", "{}\n")
    _write(
        repo,
        "tests/test_feature_flags.py",
        "PATH = 'hub/config/feature_flags.json'\n\n\ndef test_feature_flags() -> None:\n    assert PATH\n",
    )
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "hub/config/feature_flags.json", '{"a": 1}\n')

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_feature_flags.py" in selection.tests
    assert "tests/test_unrelated.py" not in selection.tests


def test_non_python_asset_unmatched_escalates_to_full(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "hub/config/mystery_asset.dat", "one\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "hub/config/mystery_asset.dat", "two\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "full"
    assert any("mystery_asset.dat" in escalation for escalation in selection.escalations)


def test_docs_only_change_is_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "docs/notes.md", "hello\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "docs/notes.md", "hello world\n")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "none"
    assert selection.tests == []
    assert selection.escalations == []


# --- untracked files / deletions ---------------------------------------------


def test_untracked_file_is_detected_as_changed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/brand_new.py", "VALUE = 1\n")  # never `git add`ed
    _write(
        repo,
        "tests/test_brand_new.py",
        "import core.brand_new\n\n\n"
        "def test_brand_new() -> None:\n    assert core.brand_new.VALUE == 1\n",
    )

    selection = affected.select_affected(repo, "main")

    assert "core/brand_new.py" in selection.changed_files
    assert "tests/test_brand_new.py" in selection.changed_files
    assert selection.mode == "subset"
    assert "tests/test_brand_new.py" in selection.tests


def test_deleted_module_selects_dependent_test(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/todelete.py", "VALUE = 1\n")
    _write(
        repo,
        "core/importer.py",
        "import core.todelete\n\n\ndef read() -> int:\n    return core.todelete.VALUE\n",
    )
    _write(
        repo,
        "tests/test_importer.py",
        "import core.importer\n\n\ndef test_importer() -> None:\n    assert True\n",
    )
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    (repo / "core" / "todelete.py").unlink()

    selection = affected.select_affected(repo, "main")

    assert "core/todelete.py" in selection.changed_files
    assert selection.mode == "subset"
    assert "tests/test_importer.py" in selection.tests
    assert "tests/test_unrelated.py" not in selection.tests


# --- base-ref resolution -------------------------------------------------------


def test_base_ref_falls_back_to_main(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="main")
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    resolved = affected.resolve_base_ref(repo, "origin/main")

    assert resolved == "main"


def test_base_ref_falls_back_to_head_minus_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")  # no "main" ref, no "origin" remote
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert True\n")
    _commit_all(repo, "first")
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert 1 == 1\n")
    _commit_all(repo, "second")

    resolved = affected.resolve_base_ref(repo, "origin/main")

    assert resolved == "HEAD~1"


def test_explicit_base_ref_used_when_present(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="main")
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert True\n")
    _commit_all(repo, "first")
    _git(repo, "branch", "feature-base")
    _write(repo, "tests/test_a.py", "def test_a() -> None:\n    assert 1 == 1\n")
    _commit_all(repo, "second")

    resolved = affected.resolve_base_ref(repo, "feature-base")

    assert resolved == "feature-base"


# --- smoke test against the real repository -----------------------------------


def test_smoke_real_repo_json_is_valid_and_fast() -> None:
    here = Path(__file__).resolve().parent
    try:
        repo_root = affected.find_repo_root(here)
    except Exception:
        pytest.skip("not running inside a git checkout")

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "core.harness.affected", "--json"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] in {"none", "subset", "full"}
    assert isinstance(payload["tests"], list)
    assert isinstance(payload["reasons"], dict)
    assert isinstance(payload["escalations"], list)
    assert isinstance(payload["uncovered"], list)
    assert elapsed < 15.0


# --- B2: uncovered changed source files ---------------------------------------


def test_uncovered_core_file_with_no_referencing_test(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/orphan.py", "VALUE = 1\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/orphan.py", "VALUE = 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.uncovered == ["core/orphan.py"]
    assert "tests/test_unrelated.py" not in selection.tests


def test_uncovered_hub_and_scripts_files_are_tracked_too(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "hub/orphan_hub.py", "VALUE = 1\n")
    _write(repo, "scripts/orphan_script.py", "VALUE = 1\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "hub/orphan_hub.py", "VALUE = 2\n")
    _write(repo, "scripts/orphan_script.py", "VALUE = 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.uncovered == ["hub/orphan_hub.py", "scripts/orphan_script.py"]


def test_uncovered_empty_when_a_test_covers_the_changed_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/leaf2.py", "VALUE = 1\n")
    _write(
        repo,
        "tests/test_leaf2.py",
        "import core.leaf2\n\n\ndef test_leaf2() -> None:\n    assert core.leaf2.VALUE == 1\n",
    )
    _commit_all(repo, "baseline")

    _write(repo, "core/leaf2.py", "VALUE = 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.uncovered == []
    assert "tests/test_leaf2.py" in selection.tests


def test_uncovered_not_tracked_for_tests_directory_changes(tmp_path: Path) -> None:
    """`tests/` helpers have their own (helper-directory) coverage rule and
    are deliberately excluded from COVERAGE_TRACKED_PREFIXES."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/helpers/util_no_users.py", "def helper() -> int:\n    return 1\n")
    _write(repo, "tests/test_alone.py", "def test_alone() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/helpers/util_no_users.py", "def helper() -> int:\n    return 2\n")

    selection = affected.select_affected(repo, "main")

    assert selection.uncovered == []


def test_uncovered_warning_printed_on_stderr_and_included_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/orphan.py", "VALUE = 1\n")
    _write(repo, "tests/test_unrelated.py", "def test_unrelated() -> None:\n    assert True\n")
    _commit_all(repo, "baseline")

    _write(repo, "core/orphan.py", "VALUE = 2\n")

    monkeypatch.chdir(repo)
    exit_code = affected.main(["--json", "--base", "main"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "[WARN] no test covers core/orphan.py" in captured.err

    payload = json.loads(captured.out)
    assert payload["uncovered"] == ["core/orphan.py"]
    # Mode stays whatever the rest of the selection computed -- an uncovered
    # file is informational, never an escalation on its own.
    assert payload["mode"] == "none"


# --- B1: run_pytest two-pass (parallel not-serial, then serial) ---------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_run_pytest_full_mode_runs_parallel_then_serial_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        calls.append(cmd)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    selection = affected.Selection(mode="full", tests=[], reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 0
    assert len(calls) == 2
    parallel_cmd, serial_cmd = calls
    assert "-n" in parallel_cmd and "auto" in parallel_cmd
    assert "--dist" in parallel_cmd and "worksteal" in parallel_cmd
    assert "not serial" in parallel_cmd
    assert "-n" not in serial_cmd  # serial pass never runs under xdist
    assert "serial" in serial_cmd and "not serial" not in serial_cmd


def test_run_pytest_serial_pass_no_tests_collected_counts_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        if "not serial" in cmd:
            return _FakeCompletedProcess(0)
        return _FakeCompletedProcess(affected.PYTEST_EXIT_NO_TESTS_COLLECTED)

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    selection = affected.Selection(mode="full", tests=[], reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 0  # exit 5 on the serial-only pass is not a failure


def test_run_pytest_combines_failure_from_serial_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        if "not serial" in cmd:
            return _FakeCompletedProcess(0)
        return _FakeCompletedProcess(1)  # a real serial failure, not "no tests"

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    selection = affected.Selection(mode="full", tests=[], reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 1


def test_run_pytest_combines_failure_from_parallel_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        calls.append(cmd)
        if "not serial" in cmd:
            return _FakeCompletedProcess(2)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    selection = affected.Selection(mode="full", tests=[], reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 2
    assert len(calls) == 2  # the serial pass still ran too, for full diagnostics


def test_run_pytest_subset_over_30_files_uses_two_pass_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        calls.append(cmd)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    tests = [f"tests/test_{i}.py" for i in range(31)]
    selection = affected.Selection(mode="subset", tests=tests, reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 0
    assert len(calls) == 2


def test_run_pytest_subset_under_30_files_runs_single_pass_no_marker_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        calls.append(cmd)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    tests = [f"tests/test_{i}.py" for i in range(5)]
    selection = affected.Selection(mode="subset", tests=tests, reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 0
    assert len(calls) == 1
    assert "not serial" not in calls[0]
    assert "serial" not in calls[0]
    assert "-n" not in calls[0]


def test_run_pytest_falls_back_to_single_pass_when_xdist_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str | None = None) -> _FakeCompletedProcess:
        calls.append(cmd)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(affected, "_xdist_available", lambda: False)
    monkeypatch.setattr(affected.subprocess, "run", fake_run)

    selection = affected.Selection(mode="full", tests=[], reasons={}, escalations=[], changed_files=["x"])
    code = affected.run_pytest(tmp_path, selection, [])

    assert code == 0
    assert len(calls) == 1
    assert "-n" not in calls[0]
    assert "not serial" not in calls[0]
    assert "serial" not in calls[0]
