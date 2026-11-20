#!/usr/bin/env python3
"""Consulta da linha ou assuncao de coordenacao, independente do harness.

    python scripts/line_briefing.py
    python scripts/line_briefing.py --json
    python scripts/line_briefing.py assumir --decisao-owner
    python scripts/line_briefing.py assumir --decisao-owner --json

A consulta mostra o coordenador (ou a causa da ausencia), as sessoes paralelas,
a fila, as cotas, os runs vivos e os PRs lidos dos provedores compartilhados.
``--decisao-owner`` troca a lease na hora e imprime o estado resultante.
Sem essa marca o comando recusa a assuncao e nao altera o registro.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.line.briefing import configure_stdio, main  # noqa: E402


if __name__ == "__main__":
    configure_stdio()
    raise SystemExit(main())
