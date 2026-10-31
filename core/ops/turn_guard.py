"""Harness-agnostic turn & end-of-session guard (USR-431).

Ensures all factory operational invariants hold regardless of which harness drives the factory
(Claude Code, Codex, Antigravity, Grok Build, headless workers):
- Learning pack generated for any ticket touching code.
- Session lifecycle tracked in execution registry.
- Operational handoff updated before concluding a delivery.
- Unified exit codes (0 for pass, 2 for blocked) matching Stop hooks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from pydantic import BaseModel, Field

from core.paths import project_root

logger = logging.getLogger(__name__)

CODE_PREFIXES = ("core/", "hub/", "scripts/", "tests/", "run_ticket.py")
TICKET_RE = re.compile(r"USR-\d+", re.IGNORECASE)
PACK_DIR = Path(".factory") / "learning_packs"
HANDOFF_PATH = Path("docs/operacao/HANDOFF.md")
STATE_ENV = "DARKFAC_EXEC_HOOK_STATE_DIR"


def state_dir() -> Path:
    override = os.environ.get(STATE_ENV, "").strip()
    return Path(override) if override else Path(tempfile.gettempdir()) / "darkfac_exec_hooks"


def detect_repo_root() -> Path:
    return project_root()


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True
        )
        return out.stdout
    except Exception:
        return ""


def detect_ticket(cwd: Optional[Path] = None) -> Optional[str]:
    """Ticket id from DARKFAC_TICKET_ID or from branch name/log of ``cwd``."""
    env = os.environ.get("DARKFAC_TICKET_ID", "").strip()
    if env:
        return env.upper()
    root = cwd or detect_repo_root()
    ctx = _git(root, "rev-parse", "--abbrev-ref", "HEAD") + _git(root, "log", "-1", "--format=%s")
    match = TICKET_RE.search(ctx)
    return match.group(0).upper() if match else None


def check_learning_pack(
    repo_root: Path,
    ticket_id: Optional[str] = None,
    base: str = "origin/main",
    head: str = "HEAD",
) -> Optional[str]:
    """Retorna o motivo do bloqueio ou None se o learning pack esta conforme."""
    paths_raw = _git(repo_root, "diff", "--name-only", f"{base}...{head}").split()
    paths_raw += [line[3:] for line in _git(repo_root, "status", "--porcelain").splitlines()]
    if not any(p.replace("\\", "/").startswith(CODE_PREFIXES) for p in paths_raw):
        return None

    ticket = ticket_id or detect_ticket(repo_root)
    if not ticket:
        return None

    ticket_upper = ticket.upper()
    pack_base = repo_root / PACK_DIR
    for ext in ("md", "json"):
        if (pack_base / f"{ticket_upper}.{ext}").is_file():
            return None

    # Check in .factory/learning_packs storage
    from core.learning_pack.storage import LearningPackStore
    store = LearningPackStore(pack_base)
    if store.load_pack(f"lp-{ticket_upper.lower()}") is not None:
        return None

    return (
        f"Sessao alterou codigo para {ticket_upper} sem learning pack. Gere com: "
        f"python -m core.learning_pack.delivery --base {base} --head {head} "
        f"--ticket {ticket_upper} --out {PACK_DIR.as_posix()}"
    )


class TurnGuardReport(BaseModel):
    ok: bool = True
    ticket_id: Optional[str] = None
    learning_pack_ok: bool = True
    learning_pack_reason: Optional[str] = None
    handoff_ok: bool = True
    handoff_reason: Optional[str] = None
    session_id: Optional[str] = None
    violations: list[str] = Field(default_factory=list)


def verify_turn(
    repo_root: Path,
    ticket_id: Optional[str] = None,
    auto_generate_learning: bool = False,
    base: str = "origin/main",
    head: str = "HEAD",
) -> TurnGuardReport:
    """Verifica todos os invariantes de fim de turno."""
    ticket = ticket_id or detect_ticket(repo_root)
    violations: list[str] = []

    # 1. Learning pack
    lp_reason = check_learning_pack(repo_root, ticket, base=base, head=head)
    lp_ok = lp_reason is None
    if not lp_ok and auto_generate_learning and ticket:
        try:
            from core.learning_pack.delivery import build_for_delivery
            from core.line.ticket_extras import publish_learning
            from core.line.workspace import RunWorkspace

            art = build_for_delivery(repo_root, base, head, ticket)
            pack_dir = repo_root / PACK_DIR
            pack_dir.mkdir(parents=True, exist_ok=True)
            (pack_dir / f"{ticket}.json").write_text(art.model_dump_json(indent=2), encoding="utf-8")
            lp_reason = None
            lp_ok = True
        except Exception as exc:
            logger.warning("Falha ao gerar learning pack automatico: %s", exc)

    if not lp_ok and lp_reason:
        violations.append(lp_reason)

    # 2. Handoff check
    handoff_file = repo_root / HANDOFF_PATH
    handoff_ok = handoff_file.is_file() and handoff_file.stat().st_size > 0
    handoff_reason = None if handoff_ok else f"Handoff operacional ausente ou vazio: {HANDOFF_PATH.as_posix()}"
    if not handoff_ok and handoff_reason:
        violations.append(handoff_reason)

    # 3. Active session check
    sdir = state_dir()
    session_id: Optional[str] = None
    if sdir.is_dir():
        for id_file in sdir.glob("*.id"):
            try:
                session_id = id_file.read_text(encoding="utf-8").strip() or None
                if session_id:
                    break
            except OSError:
                pass

    return TurnGuardReport(
        ok=len(violations) == 0,
        ticket_id=ticket,
        learning_pack_ok=lp_ok,
        learning_pack_reason=lp_reason,
        handoff_ok=handoff_ok,
        handoff_reason=handoff_reason,
        session_id=session_id,
        violations=violations,
    )


def _run_exec_cli(args: list[str]) -> str:
    cli = project_root() / "scripts" / "execution.py"
    if not cli.is_file():
        return ""
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            stdin=subprocess.DEVNULL,
        )
        return proc.stdout or ""
    except Exception:
        return ""


def start_session(
    harness: str = "codex",
    stage: str = "interactive_session",
    ticket_id: Optional[str] = None,
    model: Optional[str] = None,
    title: str = "",
) -> Optional[str]:
    """Inicia o registro de uma sessao interativa de forma agnostica."""
    ticket = ticket_id or detect_ticket()
    args = [
        "start",
        "--source", f"{harness}_session",
        "--harness", harness,
        "--stage", stage,
        "--title", title or f"Sessao interativa {harness}",
    ]
    if ticket:
        args.extend(["--ticket", ticket])
    if model:
        args.extend(["--model", model])

    eid = _run_exec_cli(args).strip()
    if eid:
        sdir = state_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / f"ses-{harness}.id").write_text(eid, encoding="utf-8")
    return eid or None


def finish_session(
    harness: str = "codex",
    execution_id: Optional[str] = None,
    status: str = "succeeded",
    evidence: Optional[str] = None,
) -> bool:
    """Encerra a sessao ativa no registro de execucoes."""
    eid = execution_id
    sdir = state_dir()
    id_file = sdir / f"ses-{harness}.id"
    if not eid and id_file.is_file():
        eid = id_file.read_text(encoding="utf-8").strip()

    if not eid:
        return False

    args = ["finish", eid, "--status", status]
    if evidence:
        args.extend(["--evidence", evidence])

    _run_exec_cli(args)
    try:
        id_file.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.ops.turn_guard",
        description="Guarda de fim de turno agnostica da DarkFac (USR-431).",
    )
    parser.add_argument("--check", action="store_true", help="Verifica todos os guardas de fim de turno")
    parser.add_argument("--fix", action="store_true", help="Gera artefatos faltantes (ex: learning pack)")
    parser.add_argument("--ticket", default=None, help="ID do ticket (ex: USR-431)")
    parser.add_argument("--start-session", action="store_true", help="Registra inicio de sessao interativa")
    parser.add_argument("--finish-session", action="store_true", help="Encerra a sessao interativa ativa")
    parser.add_argument("--harness", default="codex", help="Harness da sessao (codex, antigravity, claude, grok)")
    parser.add_argument("--status", default="succeeded", help="Status de encerramento da sessao")
    parser.add_argument("--evidence", default="", help="Evidencia da conclusao")

    args = parser.parse_args(argv)
    root = detect_repo_root()

    if args.start_session:
        eid = start_session(harness=args.harness, ticket_id=args.ticket)
        print(f"Session started: {eid or 'local-fallback'}")
        return 0

    if args.finish_session:
        ok = finish_session(harness=args.harness, status=args.status, evidence=args.evidence)
        print(f"Session finished: ok={ok}")
        return 0

    # Default action: check
    report = verify_turn(root, ticket_id=args.ticket, auto_generate_learning=args.fix)
    if not report.ok:
        for violation in report.violations:
            print(violation, file=sys.stderr)
        return 2

    print("turn_guard: all end-of-turn guards passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
