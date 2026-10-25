"""USR-216: o Hub nao pode exibir codigos internos (HF-NN, DH-NN, USR-NN, Skill NN, G1/G8).

O teste varre hub/frontend (html e js), ignora comentarios (nao visiveis) e falha se
um rotulo estatico voltar a conter esses codigos.

Permitido (nao e rotulo estatico):
- IDs de ticket como DADO renderizado de valores vindos da API (interpolacoes
  ${...} / textContent = variavel), pois o texto fonte nao contem o codigo;
- predicados de codigo que comparam ticket_id com prefixo (``startsWith("HF-26")``,
  ``[...].some(...)``), que filtram dados e nao sao exibidos;
- o placeholder do id ``grill-modal-ticket-id`` (substituido em runtime pelo
  ticket real da linha).
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "hub" / "frontend"
FORBIDDEN = re.compile(r"\b(?:HF-\d+|DH-\d+|USR-\d+|Skill \d+|G\d)\b")
ALLOWED_LINE = re.compile(r"startsWith\(|\.some\(|id=\"grill-modal-ticket-id\"")


def _visible_source(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    # Preserve line numbers while blanking comments.
    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", "", m.group(0))

    text = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return list(enumerate(text.splitlines(), start=1))


def test_frontend_labels_have_no_internal_codes() -> None:
    offenders: list[str] = []
    files = sorted(p for p in FRONTEND.glob("*") if p.suffix in {".html", ".js"})
    assert files, "hub/frontend sem arquivos html/js"
    for path in files:
        for number, line in _visible_source(path):
            if FORBIDDEN.search(line) and not ALLOWED_LINE.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:120]}")
    assert not offenders, "Codigos internos em rotulos do Hub:\n" + "\n".join(offenders)
