"""Learning pack da entrega: fatos do diff e afirmacoes verificadas."""

import subprocess
from pathlib import Path

import pytest

from core.learning_pack.delivery import build_for_delivery


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "core").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "core" / "untouched.py").write_text("def velho():\n    return 1\n")
    (tmp_path / "core" / "calc.py").write_text("X = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "core" / "calc.py").write_text("X = 1\n\ndef soma(a, b):\n    return a + b\n")
    (tmp_path / "tests" / "test_calc.py").write_text("def test_soma():\n    assert True\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "head")
    return tmp_path


def test_sem_llm_so_fatos_com_dois_niveis(repo: Path) -> None:
    art = build_for_delivery(repo, "HEAD~1", "HEAD", "USR-1", test_summary="2 passed")
    assert art.layman and art.dev
    assert "`core/calc.py`" in art.dev and "`soma`" in art.dev and "`test_soma`" in art.dev
    assert "2 passed" in art.dev
    assert art.rejected_claims == []


def test_afirmacao_que_contradiz_o_diff_e_rejeitada(repo: Path) -> None:
    def llm(_facts):
        return {
            "layman": "Agora somar ficou facil.",
            "dev": "Alteramos `core/untouched.py` nesta entrega. Adicionamos `soma` em `core/calc.py`. "
                   "Tambem criamos `inventada` na entrega.",
        }

    art = build_for_delivery(repo, "HEAD~1", "HEAD", "USR-1", llm=llm)
    reasons = " | ".join(r.reason for r in art.rejected_claims)
    assert "core/untouched.py" in reasons and "inventada" in reasons
    assert len(art.rejected_claims) == 2
    assert "untouched" not in art.dev and "core/calc.py" in art.dev
    assert art.layman == "Agora somar ficou facil."
