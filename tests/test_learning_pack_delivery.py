"""Learning pack da entrega: mudancas ainda nao commitadas entram no pack (USR-522)."""

import subprocess
from pathlib import Path

import pytest

from core.learning_pack.delivery import build_for_delivery


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo_single_commit(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "core").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "core" / "calc.py").write_text("X = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_head_igual_base_com_arvore_suja_gera_pack_nao_vazio(repo_single_commit: Path) -> None:
    repo = repo_single_commit
    # arquivo tracked modificado, sem commit
    (repo / "core" / "calc.py").write_text("X = 1\n\ndef soma(a, b):\n    return a + b\n", encoding="utf-8")
    # arquivo novo, untracked
    (repo / "tests" / "test_soma.py").write_text("def test_soma():\n    assert True\n", encoding="utf-8")

    art = build_for_delivery(repo, "HEAD", "HEAD", "USR-522")

    changes = {f.path: f for f in art.files}
    for path in ("core/calc.py", "tests/test_soma.py"):
        assert changes[path].added > 0 and changes[path].removed == 0
    assert "soma" in art.symbols
    assert "test_soma" in art.tests_changed


def test_untracked_binario_nao_quebra_a_coleta(repo_single_commit: Path) -> None:
    repo = repo_single_commit
    (repo / "core" / "blob.bin").write_bytes(b"\x00\x01\xff\xfe\x00binario\x00")

    art = build_for_delivery(repo, "HEAD", "HEAD", "USR-522")

    blob_change = next(f for f in art.files if f.path == "core/blob.bin")
    assert blob_change.added == 0 and blob_change.removed == 0
