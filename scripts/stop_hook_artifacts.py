"""Hook Stop do Claude Code: bloqueia sessao que alterou codigo sem learning pack (USR-401).

Le o JSON do hook no stdin. Se o diff contra origin/main toca codigo e a branch
ou o ultimo commit cita USR-NNN, exige `.factory/learning_packs/<USR-NNN>.md|.json`
(gerado por `python -m core.learning_pack.delivery --ticket USR-NNN --out .factory/learning_packs`).
Saida 2 + motivo em stderr bloqueia; qualquer falha inesperada libera (exit 0).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

CODE_PREFIXES = ("core/", "hub/", "scripts/", "tests/", "run_ticket.py")
TICKET_RE = re.compile(r"USR-\d+", re.IGNORECASE)
PACK_DIR = Path(".factory") / "learning_packs"


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True
    )
    return out.stdout


def missing_artifact(cwd: Path) -> str | None:
    """Retorna o motivo do bloqueio ou None se a sessao pode encerrar."""
    paths = _git(cwd, "diff", "--name-only", "origin/main...HEAD").split()
    paths += [line[3:] for line in _git(cwd, "status", "--porcelain").splitlines()]
    if not any(p.replace("\\", "/").startswith(CODE_PREFIXES) for p in paths):
        return None
    ctx = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") + _git(cwd, "log", "-1", "--format=%s")
    match = TICKET_RE.search(ctx)
    if not match:
        return None
    ticket = match.group(0).upper()
    for ext in ("md", "json"):
        if (cwd / PACK_DIR / f"{ticket}.{ext}").is_file():
            return None
    return (
        f"Sessao alterou codigo para {ticket} sem learning pack. Gere com: "
        f"python -m core.learning_pack.delivery --base origin/main --head HEAD "
        f"--ticket {ticket} --out {PACK_DIR.as_posix()}"
    )


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if data.get("stop_hook_active"):
            return 0
        reason = missing_artifact(Path(data.get("cwd") or "."))
    except Exception as exc:  # noqa: BLE001 - o hook nunca pode travar o owner
        print(f"stop_hook_artifacts: ignorado por falha inesperada: {exc}", file=sys.stderr)
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
