from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.harness import docs_sync

PAGE = "docs/manual/modulo.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def shas(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    repo = tmp_path_factory.mktemp("docs_sync_repo")
    _git(repo, "init", "-q")
    base = _commit(
        repo,
        {
            PAGE: "<!-- covers: core/a.py, pkg/*.py -->\n# Modulo\n",
            "core/a.py": "1\n", "pkg/x.py": "1\n", "tests/test_a.py": "1\n",
        },
        "base",
    )
    _git(repo, "branch", "-q", "b")
    out: dict[str, object] = {"repo": repo, "base": base}
    _git(repo, "checkout", "-q", "-b", "code_only", "b")
    out["code_only"] = _commit(repo, {"core/a.py": "2\n"}, "mexe no codigo")
    _git(repo, "checkout", "-q", "-b", "with_page", "b")
    out["with_page"] = _commit(
        repo, {"core/a.py": "3\n", PAGE: "<!-- covers: core/a.py, pkg/*.py -->\n# Modulo v2\n"}, "codigo e pagina"
    )
    _git(repo, "checkout", "-q", "-b", "trailer", "b")
    out["trailer"] = _commit(
        repo, {"core/a.py": "4\n"}, "mexe\n\nDocs-Unchanged: refatoracao interna sem efeito"
    )
    _git(repo, "checkout", "-q", "-b", "tests_only", "b")
    out["tests_only"] = _commit(repo, {"tests/test_a.py": "2\n"}, "so teste")
    _git(repo, "checkout", "-q", "-b", "glob", "b")
    out["glob"] = _commit(repo, {"pkg/x.py": "2\n"}, "mexe no glob")
    return out


def _run(shas: dict[str, object], head: str) -> list[docs_sync.Violation]:
    return docs_sync.check(Path(str(shas["repo"])), str(shas["base"]), str(shas[head]))


def test_covered_change_without_page_fails(shas):
    assert [str(v) for v in _run(shas, "code_only")] == [f"core/a.py -> {PAGE}"]


def test_covered_change_with_page_passes(shas):
    assert _run(shas, "with_page") == []


def test_trailer_with_reason_passes(shas):
    assert _run(shas, "trailer") == []


def test_tests_dir_never_triggers(shas):
    assert _run(shas, "tests_only") == []


def test_glob_matches_covered_file(shas):
    assert [v.covered_file for v in _run(shas, "glob")] == ["pkg/x.py"]


def test_dictionary_lists_tracked_json_without_entry(tmp_path):
    repo = tmp_path / "dict_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    page = "<!-- covers: docs/manual/dicionario_dados.md -->\n<!-- paths: .factory/config/*.json -->\n"
    files = {
        docs_sync.DICTIONARY_PAGE: page,
        ".factory/config/a.json": "{}\n",
        ".factory/novo/b.json": "{}\n",
    }
    _commit(repo, files, "base")
    assert docs_sync.check_dictionary(repo) == [".factory/novo/b.json"]
    assert docs_sync.main(["--dictionary", "--root", str(repo)]) == 1
