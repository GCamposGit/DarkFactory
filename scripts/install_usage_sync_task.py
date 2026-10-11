#!/usr/bin/env python3
"""Register or repair the ``DarkFac-Usage-Cloud-Sync`` scheduled task (USR-236, slice 5 of USR-206).

The task runs ``scripts/sync_usage_to_cloud.py --loop`` on the quota-meter host. It
must ALWAYS point at the main checkout, never at a worktree: on 2026-10-10 it ran
for seven days from a stale Codex worktree in SYNC_DEGRADED without anyone noticing.

Safe by default: without ``--apply`` the script only prints the plan (create / keep /
update and why). With ``--apply`` (Windows only) it registers the desired definition
through ``schtasks /Create /XML ... /F`` and verifies the result by reading it back.
The decision logic lives in the pure module ``core.usage.sync_task``.

Exit codes: 0 = ok (including keep and plan-only), 1 = registration/verification
failed, 2 = invalid argument or repository root.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Anchor sys.path to this checkout so ``core`` resolves when run as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.git.ticket_workspace import WorkspaceError, main_checkout_root
from core.usage.sync_task import (
    DEFAULT_INTERVAL_S,
    DEFAULT_TARGET_URL,
    SYNC_SCRIPT_PARTS,
    TASK_NAME,
    QueryOutcome,
    SyncTaskDefinition,
    SyncTaskPlan,
    build_task_xml,
    decode_schtasks_output,
    desired_definition,
    interpret_query,
    plan,
    worktree_marker,
)

EXIT_OK = 0
EXIT_REGISTER_FAILED = 1
EXIT_INVALID = 2

_SCHTASKS_TIMEOUT_S = 60.0

SchtasksRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class InstallArgumentError(ValueError):
    """An argument or the repository root is unusable (exit code 2)."""


# ----------------------------------------------------------------------
# schtasks plumbing (the only code that talks to the OS)
# ----------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def _oem_encoding() -> str:
    """Console (OEM) code page used by schtasks when its output is redirected."""
    if not _is_windows():
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - defensive, Windows only
        return "cp850"


def _schtasks_executable() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "schtasks.exe"
    return str(candidate) if candidate.is_file() else "schtasks"


def run_schtasks(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run ``schtasks <args>`` as an argument list (never through a shell); never raises."""
    cmd = [_schtasks_executable(), *args]
    kwargs: dict[str, Any] = {}
    if _is_windows():
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=_SCHTASKS_TIMEOUT_S,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"schtasks excedeu o tempo limite: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"schtasks indisponivel: {exc}")
    oem = _oem_encoding()
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode,
        decode_schtasks_output(proc.stdout or b"", fallback_encoding=oem),
        decode_schtasks_output(proc.stderr or b"", fallback_encoding=oem),
    )


def query_task(runner: SchtasksRunner) -> QueryOutcome:
    """Read-only: ``schtasks /Query /TN <name> /XML``."""
    result = runner(["/Query", "/TN", TASK_NAME, "/XML"])
    return interpret_query(result.returncode, result.stdout or "", result.stderr or "")


def register_task(
    definition: SyncTaskDefinition, user: str, runner: SchtasksRunner
) -> subprocess.CompletedProcess[str]:
    """``schtasks /Create /TN <name> /XML <utf-16 file> /F`` (creates or replaces the task)."""
    xml = build_task_xml(definition, user)
    with tempfile.TemporaryDirectory(prefix="darkfac-usage-sync-") as tmp:
        xml_path = Path(tmp) / f"{TASK_NAME}.xml"
        xml_path.write_text(xml, encoding="utf-16")  # BOM + UTF-16, as Task Scheduler expects
        return runner(["/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])


def current_user() -> str:
    name = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{name}" if domain else name


# ----------------------------------------------------------------------
# Argument resolution
# ----------------------------------------------------------------------


def _refuse_worktree(path_text: str) -> None:
    marker = worktree_marker(path_text)
    if marker:
        raise InstallArgumentError(
            f"o caminho esta dentro de uma worktree ({marker}): {path_text}. "
            "A tarefa deve apontar para o checkout principal (por exemplo C:\\dev\\DarkFac)."
        )


def resolve_repo_root(raw: str | None) -> Path:
    """The main-checkout root the task will point at; never a worktree.

    Without ``--repo-root`` it is derived from THIS script's repository through
    ``git worktree list`` (first entry), so running the installer from a worktree
    still yields the main checkout. The process working directory is never used.
    """
    if raw:
        candidate = Path(raw).expanduser()
        _refuse_worktree(str(candidate))
        path = candidate.resolve()
    else:
        try:
            path = Path(main_checkout_root(REPO_ROOT)).resolve()
        except WorkspaceError as exc:
            raise InstallArgumentError(
                f"nao foi possivel localizar o checkout principal ({exc}); informe --repo-root."
            ) from exc
    _refuse_worktree(str(path))
    if (path / ".git").is_file():
        raise InstallArgumentError(
            f"{path} e uma worktree vinculada (.git e um arquivo); use o checkout principal."
        )
    script = path.joinpath(*SYNC_SCRIPT_PARTS)
    if not script.is_file():
        raise InstallArgumentError(f"script de sincronizacao nao encontrado: {script}")
    return path


def resolve_python(raw: str | None, *, check_exists: bool) -> str:
    """``--python`` or ``pythonw.exe`` next to the running interpreter (windowless task)."""
    if raw and raw.strip():
        candidate = raw.strip()
        if check_exists and not Path(candidate).is_file():
            raise InstallArgumentError(f"--python nao existe: {candidate}")
        return candidate
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if not sibling.is_file():
        raise InstallArgumentError(
            f"pythonw.exe nao encontrado ao lado de {sys.executable}; informe --python <caminho do pythonw.exe>."
        )
    return str(sibling)


# ----------------------------------------------------------------------
# Execution and reporting
# ----------------------------------------------------------------------


@dataclass
class InstallReport:
    exit_code: int
    mode: str  # "plan" | "apply"
    repo_root: str = ""
    plan: SyncTaskPlan | None = None
    query_note: str = ""
    applied: bool = False
    verified: bool | None = None
    error: str = ""
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": TASK_NAME,
            "mode": self.mode,
            "exit_code": self.exit_code,
            "repo_root": self.repo_root,
            "action": self.plan.action if self.plan else None,
            "reasons": list(self.plan.reasons) if self.plan else [],
            "desired": asdict(self.plan.desired) if self.plan else None,
            "existing": asdict(self.plan.existing) if self.plan and self.plan.existing else None,
            "query_note": self.query_note,
            "applied": self.applied,
            "verified": self.verified,
            "error": self.error,
            "hints": list(self.hints),
        }


def _restart_hints(action: str) -> list[str]:
    stop = f'schtasks /End /TN "{TASK_NAME}"'
    run = f'schtasks /Run /TN "{TASK_NAME}"'
    if action == "update":
        return [
            "Re-registrar a tarefa NAO interrompe uma instancia que ja esteja rodando com o caminho antigo.",
            f"Para trocar o processo agora: {stop}  e depois  {run}",
        ]
    return [f"Para iniciar sem esperar o proximo logon: {run}"]


def execute(
    args: argparse.Namespace,
    *,
    runner: SchtasksRunner | None = None,
    user: str | None = None,
) -> InstallReport:
    """Resolve the desired definition, plan, and (with ``--apply``) register and verify."""
    mode = "apply" if args.apply else "plan"
    real_runner = runner is None
    if args.apply and real_runner and not _is_windows():
        return InstallReport(EXIT_INVALID, mode, error="--apply so funciona no Windows (Task Scheduler).")

    try:
        root = resolve_repo_root(args.repo_root)
        python_exe = resolve_python(args.python, check_exists=bool(args.apply and real_runner))
        desired = desired_definition(
            root, python_exe=python_exe, target_url=args.target_url, interval=args.interval
        )
    except ValueError as exc:  # InstallArgumentError is a ValueError
        return InstallReport(EXIT_INVALID, mode, error=str(exc))

    schtasks = runner or run_schtasks
    outcome = query_task(schtasks)
    decision = plan(outcome.existing, desired, unreadable=outcome.unreadable)
    report = InstallReport(
        EXIT_OK, mode, repo_root=str(root), plan=decision, query_note=outcome.note
    )
    if not args.apply or decision.action == "keep":
        return report

    account = user or current_user()
    result = register_task(desired, account, schtasks)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"codigo {result.returncode}"
        report.exit_code = EXIT_REGISTER_FAILED
        report.error = (
            f"schtasks /Create falhou: {detail}. Se a mensagem for de acesso negado, "
            "repita em um PowerShell aberto como Administrador."
        )
        return report
    report.applied = True

    after = query_task(schtasks)
    check = plan(after.existing, desired, unreadable=after.unreadable)
    report.verified = check.action == "keep"
    if not report.verified:
        report.exit_code = EXIT_REGISTER_FAILED
        detail = "; ".join(check.reasons) or "tarefa nao encontrada"
        report.error = f"a tarefa foi registrada, mas a verificacao posterior divergiu: {detail}"
        return report
    report.hints = _restart_hints(decision.action)
    return report


def render_text(report: InstallReport) -> str:
    lines = [f"Tarefa: {TASK_NAME}"]
    if report.mode == "plan":
        lines.append("Modo: plano (somente leitura; nada foi alterado. Use --apply para registrar)")
    else:
        lines.append("Modo: apply")
    if report.repo_root:
        lines.append(f"Repositorio: {report.repo_root}")
    if report.plan is not None:
        lines.append(f"Acao: {report.plan.action}")
        for reason in report.plan.reasons:
            lines.append(f"  - {reason}")
        desired = report.plan.desired
        lines.extend(
            [
                "Desejado:",
                f"  executavel: {desired.command}",
                f"  argumentos: {desired.arguments}",
                f"  diretorio:  {desired.working_directory}",
            ]
        )
        existing = report.plan.existing
        if existing is not None:
            lines.extend(
                [
                    "Atual:",
                    f"  executavel: {existing.command}",
                    f"  argumentos: {existing.arguments}",
                    f"  diretorio:  {existing.working_directory}",
                ]
            )
    if report.query_note:
        lines.append(f"Consulta: {report.query_note}")
    if report.mode == "apply" and not report.error and report.plan is not None:
        if report.plan.action == "keep":
            lines.append("Resultado: nada a fazer (a tarefa ja esta correta).")
        elif report.applied:
            lines.append("Resultado: tarefa registrada e verificada (leitura posterior igual ao desejado).")
    lines.extend(report.hints)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"Plan or register the {TASK_NAME} scheduled task so it always points at the "
            "main checkout. Without --apply it only prints the plan."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Main checkout root (default: main checkout of this repository, never the cwd or a worktree).",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Absolute path of pythonw.exe (default: pythonw.exe next to the running interpreter).",
    )
    parser.add_argument("--target-url", default=DEFAULT_TARGET_URL, help=f"DarkHub URL (default: {DEFAULT_TARGET_URL}).")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        help=f"Sync interval in seconds (default: {DEFAULT_INTERVAL_S}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Register/replace the task (Windows only). Without it only the plan is printed.",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: SchtasksRunner | None = None,
    user: str | None = None,
) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse: 0 for --help, 2 for bad arguments
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_INVALID

    report = execute(args, runner=runner, user=user)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=True))
    else:
        print(render_text(report))
        if report.error:
            print(f"erro: {report.error}", file=sys.stderr)
    return report.exit_code


def _configure_stdio() -> None:
    """UTF-8 on the Windows console/pipes (Windows hardening rule); best effort."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _configure_stdio()
    sys.exit(main())
