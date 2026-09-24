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
    assert elapsed < 15.0
