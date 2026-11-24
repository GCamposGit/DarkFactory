"""Unit tests for the stateless test-impact selector (core/harness/affected.py).

Every scenario builds a minimal synthetic git repository under ``tmp_path``
that mirrors the real repo's scanned layout (``core/``, ``hub/``, ``tests/``)
so the selector's hardcoded scan roots apply. Tests call
:func:`core.harness.affected.select_affected` directly against that
synthetic repo -- no dependency on the real DarkFac test suite except for
the dedicated smoke test at the bottom.
"""

from __future__ import annotations

import ast
import json
import os
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


def test_repository_guard_selected_for_unrelated_new_test(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_clock_bomb_guard.py",
        "import pytest\n\npytestmark: object = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n",
    )
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_new_unrelated.py" in selection.tests
    assert "tests/test_clock_bomb_guard.py" in selection.tests
    assert "repository_guard" in selection.reasons["tests/test_clock_bomb_guard.py"]


def test_repository_guard_selected_when_no_files_changed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_clock_bomb_guard.py",
        "import pytest\n\npytestmark = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n",
    )
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _commit_all(repo, "baseline")

    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert selection.tests == ["tests/test_clock_bomb_guard.py"]
    assert selection.reasons["tests/test_clock_bomb_guard.py"] == ["repository_guard"]


def test_repository_guard_selected_when_new_test_contains_fixed_2026_date(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_clock_bomb_guard.py",
        "import pytest\n\npytestmark = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n",
    )
    _commit_all(repo, "baseline")

    fixed_date = "2026" + "-10-25"
    _write(
        repo,
        "tests/test_new_date.py",
        "def test_new():\n    date = " + repr(fixed_date) + "\n    assert date\n",
    )
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert "tests/test_new_date.py" in selection.tests
    assert "tests/test_clock_bomb_guard.py" in selection.tests
    assert "repository_guard" in selection.reasons["tests/test_clock_bomb_guard.py"]


def test_guard_named_file_without_marker_is_not_selected_as_repository_guard(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_fake_guard.py", "def test_guard():\n    pass\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert "tests/test_fake_guard.py" not in selection.tests
    assert "repository_guard" not in selection.reasons.get("tests/test_fake_guard.py", [])


def test_repository_guard_marker_in_text_only_is_not_selected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_marker_text_decoy.py",
        '"""The string pytest.mark.repository_guard appears in this docstring."""\n'
        "# pytest.mark.repository_guard is mentioned here, not applied.\n"
        "def test_decoy():\n    pass\n",
    )
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert selection.tests == ["tests/test_new_unrelated.py"]
    assert "repository_guard" not in selection.reasons.get(
        "tests/test_marker_text_decoy.py", []
    )


def test_repository_guard_marker_nested_in_other_pytestmark_expression_is_ignored(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_marker_decoy.py",
        "import pytest\n\n"
        "def wrap(marker):\n    return marker\n\n"
        "pytestmark = wrap(pytest.mark.repository_guard)\n\n"
        "def test_decoy():\n    pass\n",
    )
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert "tests/test_marker_decoy.py" not in selection.tests
    assert "repository_guard" not in selection.reasons.get("tests/test_marker_decoy.py", [])


def test_repository_guard_marker_in_nested_pytestmark_assignment_is_ignored(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_marker_decoy.py",
        "import pytest\n\n"
        "def helper():\n"
        "    pytestmark = pytest.mark.repository_guard\n\n"
        "def test_decoy():\n"
        "    pass\n",
    )
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert "tests/test_marker_decoy.py" not in selection.tests
    assert "repository_guard" not in selection.reasons.get("tests/test_marker_decoy.py", [])


def test_repository_guard_marker_referenced_only_by_dynamic_name_is_ignored(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_marker_decoy.py",
        "import pytest\n\n"
        "marker = pytest.mark.repository_guard\n"
        "pytestmark = marker\n\n"
        "@marker\n"
        "def test_decoy():\n"
        "    pass\n",
    )
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    pass\n")
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert selection.tests == ["tests/test_new_unrelated.py"]
    assert "repository_guard" not in selection.reasons.get("tests/test_marker_decoy.py", [])


_NAMED_REPOSITORY_GUARDS = (
    "tests/test_clock_bomb_guard.py",
    "tests/test_serial_marker_guard.py",
    "tests/test_tree_hygiene_guard.py",
    "tests/test_skills_drift.py",
    "tests/test_model_id_ratchet_usr185.py",
)


def test_global_repository_guards_declare_marker_and_skills_keeps_offline() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    for rel in _NAMED_REPOSITORY_GUARDS:
        source = (repository_root / rel).read_text(encoding="utf-8")
        assert affected._has_repository_guard_marker(source, rel)

    # The three guards covered by USR-390 T2 must opt in at module scope, so
    # the PR selector cannot depend on individual test names or decorators.
    for rel in (
        "tests/test_clock_bomb_guard.py",
        "tests/test_serial_marker_guard.py",
        "tests/test_tree_hygiene_guard.py",
    ):
        tree = ast.parse((repository_root / rel).read_text(encoding="utf-8"))
        module_marks = [
            statement.value
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in statement.targets
            )
        ]
        assert any(
            isinstance(mark, ast.Attribute)
            and mark.attr == "repository_guard"
            and isinstance(mark.value, ast.Attribute)
            and mark.value.attr == "mark"
            for mark in module_marks
        ), f"{rel} must declare pytestmark = pytest.mark.repository_guard at module scope"

    ratchet_source = (repository_root / "tests/test_model_id_ratchet_usr185.py").read_text(
        encoding="utf-8"
    )
    ratchet_tree = ast.parse(ratchet_source)
    ratchet_pytestmark = next(
        statement.value
        for statement in ratchet_tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        )
    )
    assert isinstance(ratchet_pytestmark, ast.Attribute)
    assert ratchet_pytestmark.attr == "repository_guard"

    skills_guard = (repository_root / "tests/test_skills_drift.py").read_text(encoding="utf-8")
    tree = ast.parse(skills_guard)
    module_marks = {
        node.attr
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets)
        for node in ast.walk(statement.value)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    }
    assert {"offline", "repository_guard"} <= module_marks


def _assert_named_guards_selected(selection: affected.Selection) -> None:
    assert selection.mode == "subset"
    for rel in _NAMED_REPOSITORY_GUARDS:
        assert rel in selection.tests
        assert "repository_guard" in selection.reasons[rel]


def test_pr_profile_selects_explicit_repository_guards_regardless_of_changed_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_clock_bomb_guard.py",
        "import pytest\n\npytestmark = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n",
    )
    for rel in _NAMED_REPOSITORY_GUARDS[1:]:
        if rel == "tests/test_skills_drift.py":
            body = (
                "import pytest\n\n"
                "pytestmark = [pytest.mark.offline, pytest.mark.repository_guard]\n\n"
                "def test_scan():\n    pass\n"
            )
        else:
            body = "import pytest\n\npytestmark = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n"
        _write(repo, rel, body)
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _commit_all(repo, "baseline")

    _write(repo, "tests/test_new_unrelated.py", "def test_new():\n    assert True\n")
    by_test = affected.select_affected(repo, "main")
    _assert_named_guards_selected(by_test)
    assert "tests/test_new_unrelated.py" in by_test.tests
    assert "tests/test_existing.py" not in by_test.tests

    _write(repo, "core/unrelated_leaf.py", "VALUE = 1\n")
    by_source = affected.select_affected(repo, "main")
    _assert_named_guards_selected(by_source)
    assert "tests/test_new_unrelated.py" in by_source.tests
    assert "tests/test_existing.py" not in by_source.tests
    assert "core/unrelated_leaf.py" in by_source.uncovered


def test_docs_change_selects_marked_repository_guards_without_full_suite(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_clock_bomb_guard.py",
        "import pytest\n\npytestmark = pytest.mark.repository_guard\n\ndef test_scan():\n    pass\n",
    )
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _write(repo, "docs/notes.md", "hello\n")
    _commit_all(repo, "baseline")

    _write(repo, "docs/notes.md", "hello world\n")
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert selection.escalations == []
    assert selection.tests == ["tests/test_clock_bomb_guard.py"]
    assert selection.reasons["tests/test_clock_bomb_guard.py"] == ["repository_guard"]


def test_pr_profile_discovers_direct_repository_guard_decorators(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(
        repo,
        "tests/test_class_guard.py",
        "import pytest\n\n"
        "@pytest.mark.repository_guard\n"
        "class TestRepositoryScan:\n"
        "    def test_scan(self):\n        pass\n",
    )
    _write(
        repo,
        "tests/test_function_guard.py",
        "import pytest\n\n"
        "@pytest.mark.repository_guard()\n"
        "def test_scan():\n    pass\n",
    )
    _write(
        repo,
        "tests/test_method_guard.py",
        "import pytest\n\n"
        "class TestRepositoryScan:\n"
        "    @pytest.mark.repository_guard\n"
        "    def test_scan(self):\n        pass\n",
    )
    _write(repo, "tests/test_existing.py", "def test_existing():\n    pass\n")
    _commit_all(repo, "baseline")

    _write(repo, "docs/notes.md", "notes\n")
    selection = affected.select_affected(repo, "main")

    assert selection.mode == "subset"
    assert selection.escalations == []
    for rel in (
        "tests/test_class_guard.py",
        "tests/test_function_guard.py",
        "tests/test_method_guard.py",
    ):
        assert rel in selection.tests
        assert "repository_guard" in selection.reasons[rel]
    assert "tests/test_existing.py" not in selection.tests


def test_repository_guard_marker_is_registered_for_strict_markers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    ini_text = (repo_root / "pytest.ini").read_text(encoding="utf-8")
    assert "repository_guard:" in ini_text
    assert "--strict-markers" in ini_text

    target = tmp_path / "test_repository_guard_marker.py"
    target.write_text(
        "import pytest\n\n"
        "pytestmark = pytest.mark.repository_guard\n\n"
        "def test_marked():\n"
        "    pass\n",
        encoding="utf-8",
    )
    isolated_ini = tmp_path / "pytest.ini"
    isolated_ini.write_text(ini_text, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "-c",
            str(isolated_ini),
            "-o",
            "addopts=--strict-markers",
            "--collect-only",
            "-q",
            str(target),
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
    # pr profile (USR-377 S5): docs-only runs no affected tests, only smoke.
    assert affected.run_pytest(repo, selection, []) == 0

    # ...but a change to harness.config.json escalates to the full suite.
    _write(repo, "harness.config.json", "{}\n")
    assert affected.select_affected(repo, "main").mode == "full"

    from core.harness import runner

    config, _ = runner.load_config(Path(__file__).resolve().parents[1] / "harness.config.json")
    pr_names = [s.name for s in runner.config_for_profile(config, "pr").steps]
    assert set(pr_names) == {"syntax_only", "static_types", "layer_contracts", "complexity_ratchet", "affected_tests", "smoke_tests"}
    full_names = [s.name for s in runner.config_for_profile(config, "full").steps]
    assert "affected_tests" not in full_names and "smoke_tests" not in full_names


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


def test_shallow_detached_checkout_rejects_unavailable_base(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source, branch="trunk")
    _write(source, "tests/test_a.py", "def test_a() -> None:\n    assert True\n")
    _write(source, "tests/test_canaletto.py", "def test_local() -> None:\n    assert False\n")
    _commit_all(source, "first")
    _write(source, "core/example.py", "VALUE = 1\n")
    _commit_all(source, "second")
    repo = tmp_path / "clone"
    _git(tmp_path, "clone", "--depth=1", source.as_uri(), str(repo))
    _git(repo, "checkout", "--detach")
    _git(repo, "branch", "-D", "trunk")

    # The base-owned harness fails closed when no trustworthy diff exists.
    # Sidebar changes must not introduce a governance fallback here.
    with pytest.raises(affected.GraphBuildError, match="no usable base ref"):
        affected.select_affected(repo, "origin/main")


def test_unborn_checkout_still_rejects_missing_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    with pytest.raises(affected.GraphBuildError, match="no usable base ref"):
        affected.select_affected(repo, "origin/main")


# The official quick gate runs this test in the serial step so xdist workers do
# not make the absolute latency budget depend on unrelated CPU contention.
@pytest.mark.serial
@pytest.mark.parametrize("shallow_detached", [False, True])
def test_smoke_real_repo_json_is_valid_and_fast(
    tmp_path: Path, shallow_detached: bool
) -> None:
    here = Path(__file__).resolve().parent
    try:
        repo_root = affected.find_repo_root(here)
    except Exception:
        pytest.skip("not running inside a git checkout")

    if shallow_detached:
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "--depth=1", "--no-local", str(repo_root), str(clone))
        _git(clone, "checkout", "--detach")
        assert _git(clone, "rev-parse", "--is-shallow-repository").strip() == "true"
        repo_root = clone

    started = time.monotonic()
    result = subprocess.run(
        # This smoke checks CLI output and latency, not branch discovery.
        # HEAD exists even in the validation stage's shallow detached clone;
        # missing-base rejection is covered independently above.
        [sys.executable, "-m", "core.harness.affected", "--base", "HEAD", "--json"],
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
    assert payload["mode"] in {"none", "subset", "full", "affected_fallback"}
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
    assert payload["mode"] == "affected_fallback"


# --- B1: run_pytest two-pass (parallel not-serial, then serial) ---------------



@pytest.mark.parametrize("gib, workers, expected_code", [(1.49, 0, 75), (1.5, 1, 0), (3.9, 1, 0), (8.0, 3, 0)])
def test_run_pytest_memory_budget_and_parallel_split(monkeypatch, tmp_path, gib, workers, expected_code):
    from core.line import gate_ram

    calls = []
    monkeypatch.setattr(gate_ram, "measure_workers", lambda scope="full": gate_ram.RamReading(gib, workers))
    monkeypatch.setattr(affected, "_xdist_available", lambda: True)
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        output = "\n".join(f"tests/test_a.py::test_x[{i}]" for i in range(4)) if "--collect-only" in cmd else "4 passed in 0.1s"
        return subprocess.CompletedProcess(cmd, 0, stdout=output)
    monkeypatch.setattr(affected.subprocess, "run", run)
    selection = affected.Selection("subset", [f"tests/test_{i}.py" for i in range(31)], {}, [], ["x"])
    assert affected.run_pytest(tmp_path, selection, []) == expected_code
    if expected_code:
        assert calls == []
    else:
        assert calls[0][0][-1] == "--collect-only"
        assert 198 < calls[1][1]["timeout"] <= 200.5  # resto do prazo: tolera arredondamento do relogio
        assert calls[1][0][calls[1][0].index("-n") + 1] == ("3" if workers > 1 else "0")
        assert len(calls) == (3 if workers > 1 else 2)


def test_affected_collection_and_execution_share_the_parent_deadline(monkeypatch, tmp_path, capsys):
    from core.line import gate_ram

    monkeypatch.setattr(gate_ram, "measure_workers", lambda scope="full": gate_ram.RamReading(2.0, 1))
    clock = [0.0]
    monkeypatch.setattr(affected.time, "monotonic", lambda: clock[0])

    def run(cmd, **kwargs):
        if "--collect-only" in cmd:
            assert kwargs["timeout"] == 12
            clock[0] = 5.0
            return subprocess.CompletedProcess(cmd, 0, stdout="tests/test_probe.py::test_probe")
        assert kwargs["timeout"] == 7
        raise subprocess.TimeoutExpired(cmd, 7, output=b"test_probe started")

    monkeypatch.setattr(affected.subprocess, "run", run)
    selection = affected.Selection("subset", ["tests/test_probe.py"], {}, [], ["probe.py"])

    assert affected.run_pytest(tmp_path, selection, [], deadline=12) == 124
    output = capsys.readouterr().out
    assert "[TEST_PHASE] collection files=1 timeout_sec=12" in output
    assert "test_probe started\n[STEP_TIMEOUT] affected tests phase=execution" in output


@pytest.mark.parametrize(
    "file_count,remaining,expected",
    [(23, 1000, 350), (100, 1000, 600), (23, 75, 75)],
)
def test_selected_collection_timeout_scales_and_respects_caps(file_count, remaining, expected):
    assert affected.selected_collection_timeout(file_count, remaining) == expected


def test_affected_collection_reports_scaled_timeout_for_23_files(monkeypatch, tmp_path, capsys):
    from core.line import gate_ram

    monkeypatch.setattr(gate_ram, "measure_workers", lambda scope="full": gate_ram.RamReading(2.0, 1))
    monkeypatch.setattr(affected, "_xdist_available", lambda: False)
    monkeypatch.setattr(affected.time, "monotonic", lambda: 0.0)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "--collect-only" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="tests/test_probe.py::test_probe")
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed")

    monkeypatch.setattr(affected.subprocess, "run", run)
    selection = affected.Selection("subset", [f"tests/test_{i}.py" for i in range(23)], {}, [], ["probe.py"])

    assert affected.run_pytest(tmp_path, selection, [], deadline=1000) == 0
    assert calls[0][1]["timeout"] == 350
    assert "[TEST_PHASE] collection files=23 timeout_sec=350" in capsys.readouterr().out


def test_affected_collection_timeout_returns_124_with_phase(monkeypatch, tmp_path, capsys):
    from core.line import gate_ram

    monkeypatch.setattr(gate_ram, "measure_workers", lambda scope="full": gate_ram.RamReading(2.0, 1))
    monkeypatch.setattr(affected, "_xdist_available", lambda: False)
    monkeypatch.setattr(affected.time, "monotonic", lambda: 0.0)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=b"collection started")

    monkeypatch.setattr(affected.subprocess, "run", run)
    selection = affected.Selection("subset", [f"tests/test_{i}.py" for i in range(23)], {}, [], ["probe.py"])

    assert affected.run_pytest(tmp_path, selection, [], deadline=1000) == 124
    output = capsys.readouterr().out
    assert calls[0][1]["timeout"] == 350
    assert "[TEST_PHASE] collection files=23 timeout_sec=350" in output
    assert "collection started\n[STEP_TIMEOUT] affected tests phase=collection" in output


@pytest.mark.parametrize("changed", ["tests/conftest.py", "conftest.py", "tests/line/conftest.py", "tests/_helper.py", "pytest.ini", "harness.config.json", "requirements.txt"])
def test_pr_global_change_runs_fixed_smoke_instead_of_full(tmp_path, changed, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_a.py", "def test_a(): assert True\n")
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, changed, "# changed\n")
    selection = affected.select_pr_tests(repo)
    assert selection.mode == "affected_fallback"
    assert selection.tests == list(affected.PR_SMOKE_TESTS)
    assert affected.selected_test_timeout(1000) == 1800


def test_pr_diff_keeps_earlier_branch_commits(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "tests/test_a.py", "def test_a(): assert True\n")
    _write(repo, "tests/test_b.py", "def test_b(): assert True\n")
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-b", "feature")
    _write(repo, "tests/test_a.py", "def test_a(): assert 1\n")
    _commit_all(repo, "first change")
    _write(repo, "tests/test_b.py", "def test_b(): assert 2\n")
    _commit_all(repo, "second change")
    selection = affected.select_pr_tests(repo)
    assert selection.mode == "subset"
    assert selection.tests == ["tests/test_a.py", "tests/test_b.py"]
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert affected.select_pr_tests(repo).mode == "affected_fallback"

def test_real_pytest_counts_parametrized_selection_and_runs_serial(monkeypatch, tmp_path, capsys):
    from core.line import gate_ram

    _write(tmp_path, "tests/test_probe.py", "import pytest\n@pytest.mark.parametrize('n', [1, 2, 3])\ndef test_probe(n): assert n > 0\n")
    monkeypatch.setattr(gate_ram, "measure_workers", lambda scope="full": gate_ram.RamReading(2.0, 1))
    selection = affected.Selection("subset", ["tests/test_probe.py"], {}, [], ["tests/test_probe.py"])
    assert affected.run_pytest(tmp_path, selection, ["-p", "no:cacheprovider"]) == 0
    output = capsys.readouterr().out
    assert "selected=3 timeout_sec=180 workers=0" in output
    assert "3 passed" in output


def test_pr_fallback_keeps_contracts_and_tests_citing_changed_module(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/line/agent_cli.py", "VALUE = 1\n")
    _write(repo, "tests/test_a.py", "def test_a(): assert True\n")
    _write(repo, "tests/line/test_citer.py", "def test_c():\n    target = 'core.line.agent_cli.build'\n")
    _write(repo, "tests/test_model_id_ratchet_usr185.py", "def test_r(): assert True\n")
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, "tests/conftest.py", "# global\n")  # forca o fallback
    _write(repo, "core/line/agent_cli.py", "VALUE = 2\n")
    selection = affected.select_pr_tests(repo)
    assert selection.mode == "affected_fallback"
    assert {"tests/test_model_id_ratchet_usr185.py", "tests/line/test_citer.py",
            *affected.PR_SMOKE_TESTS} <= set(selection.tests)
    assert "tests/test_a.py" not in selection.tests


def test_pr_subset_always_adds_contract_tests_for_docs_only_diff(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "docs/manual/custo.md", "a\n")
    _write(repo, "tests/test_model_id_ratchet_usr185.py", "def test_r(): assert True\n")
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, "docs/manual/custo.md", "b\n")
    selection = affected.select_pr_tests(repo)
    assert "tests/test_model_id_ratchet_usr185.py" in selection.tests


def test_demands_ledger_change_selects_only_hygiene_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, ".factory/demands/demands.json", '{"tickets": []}\n')
    _write(repo, "tests/test_demands_hygiene.py", "def test_hygiene(): assert True\n")
    _write(repo, "tests/test_ledger_hygiene.py", "def test_ledger(): assert True\n")
    _write(repo, "tests/test_unrelated_fixture.py", 'PATH = ".factory/demands/demands.json"\ndef test_u(): assert True\n')
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, ".factory/demands/demands.json", '{"tickets": [{"id": "USR-1"}]}\n')

    selection = affected.select_pr_tests(repo)
    assert "tests/test_demands_hygiene.py" in selection.tests
    assert "tests/test_ledger_hygiene.py" in selection.tests
    assert "tests/test_unrelated_fixture.py" not in selection.tests


def test_pr_fallback_caps_hot_module_cited_explosion_while_keeping_changed_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "core/hot_mod.py", "X = 1\n")
    _write(repo, "core/focused_mod.py", "Y = 1\n")
    for idx in range(35):
        _write(repo, f"tests/test_hot_{idx:02d}.py", "def test_h():\n    ref = 'core.hot_mod'\n")
    _write(repo, "tests/test_focused.py", "def test_f():\n    ref = 'core.focused_mod'\n")
    _write(repo, "tests/test_direct_edit.py", "def test_d(): assert True\n")
    _write(repo, "tests/test_model_id_ratchet_usr185.py", "def test_r(): assert True\n")
    _commit_all(repo, "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, "tests/conftest.py", "# global\n")
    _write(repo, "core/hot_mod.py", "X = 2\n")
    _write(repo, "core/focused_mod.py", "Y = 2\n")
    _write(repo, "tests/test_direct_edit.py", "def test_d(): assert 1 == 1\n")

    selection = affected.select_pr_tests(repo)
    assert selection.mode == "affected_fallback"
    assert "tests/test_direct_edit.py" in selection.tests
    assert "tests/test_focused.py" in selection.tests
    assert "tests/test_model_id_ratchet_usr185.py" in selection.tests
    assert any(item.startswith("fallback_cited_capped:") for item in selection.escalations)
    hot_selected = [t for t in selection.tests if t.startswith("tests/test_hot_")]
    assert len(hot_selected) == affected.PR_FALLBACK_MAX_CITED_FILES - 1
