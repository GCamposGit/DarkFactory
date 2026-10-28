"""Helpers por estagio do launcher `run_ticket.py` (USR-375 S3).

Refatoracao pura: o comportamento, as mensagens e os codigos de saida sao os mesmos de antes da
divisao de `_run`, `_run_gate_with_self_correction` e `_deliver_worktree`. Este modulo guarda a
parte sem dependencia de globais que os testes substituem em `run_ticket` (construtores de prompt,
de comando/ambiente da entrega, de payloads e de relatorios de falha). O fluxo de controle que
chama funcoes substituiveis (`pick`, `inspect_quotas`, `_run_bounded`...) continua em `run_ticket.py`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.line import line_lock, local_cancel
from core.line.diagnostics import redacted_head
from core.line.local_progress import ENV_RUN_ID, ENV_WARNED
from core.workflow import execution_registry

logger = logging.getLogger("darkfac.run_ticket")

# Resultados de um passo do laco de autocorrecao do portao.
STEP_PROCEED = "proceed"
STEP_CONTINUE = "continue"
STEP_BREAK = "break"
STEP_CANCEL = "cancel"
STEP_PASS = "pass"


@dataclass
class GateState:
    """Estado mutavel do laco de autocorrecao (focal + portao oficial)."""

    attempt: int = 0
    last_output: str = ""
    last_cause: str = "gate_failed"
    last_exit_code: int = 1
    failed_tests: list[str] = field(default_factory=list)
    gate_log_tail: str = ""
    last_gate_worker: Optional[str] = None


def infra_error_entry(attempt: int, stage: str, cause: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"event": "infra_error", "attempt": attempt + 1, "stage": stage, "cause": cause}
    entry.update(extra)
    return entry


def reinvocation_entry(
    attempt: int, stage: str, cause: str, fix_result: Any, limit_s: float, changed: Any
) -> dict[str, Any]:
    return {
        "event": "reinvocation", "attempt": attempt + 1, "stage": stage, "cause": cause,
        "duration_s": round(fix_result.duration_s, 1) if fix_result else 0.0,
        "limit_s": limit_s, "changed": changed,
    }


# ---------------------------------------------------------------- gate de projeto externo
def external_project_gate(
    ticket: Any, target_worktree: Path, skip_validation: bool, as_json: bool, progress: Any,
    gate_timeout_s: float,
) -> tuple[bool, Any]:
    """Portao declarado por projeto externo. Retorna (tratado, retorno): tratado=False => projeto darkfac."""
    from core.projects.registry import get_project_registry
    from core.projects.ticket_execution import ProjectExecutionError, validate_project_commands
    from core.line.agent_cli import redact_secrets

    project = get_project_registry().get_project(ticket.project_id)
    if project is None:
        detail = f"Projeto '{ticket.project_id}' não existe em .factory/projects.json."
        progress.phase("gate", "failed", detail, cause="project_not_registered")
        return True, {"exit_code": 1, "ok": False, "cause": "project_not_registered", "error": detail}
    if not project.commands.validate_cmds:
        detail = (
            f"Projeto {project.id} não declara commands.validate. Execute o Project Adoption Gateway "
            "(skill 07) no onboarding e declare comandos de validação antes de executar tickets."
        )
        progress.phase("gate", "failed", detail, cause="project_validate_missing")
        return True, {"exit_code": 1, "ok": False, "cause": "project_validate_missing", "error": detail}
    failure_cause = "project_validate_failed"
    try:
        report = validate_project_commands(project, target_worktree, timeout_s=int(gate_timeout_s))
        if report.ok and project.commands.build:
            from core.projects.ticket_execution import run_project_commands
            failure_cause = "project_build_failed"
            report = run_project_commands(project.commands.build, target_worktree, timeout_s=int(gate_timeout_s), project=project)
        if report.ok and project.commands.smoke:
            from core.projects.ticket_execution import run_project_commands
            failure_cause = "project_smoke_failed"
            report = run_project_commands(project.commands.smoke, target_worktree, timeout_s=int(gate_timeout_s), project=project)
    except ProjectExecutionError as exc:
        detail = str(exc)
        progress.phase("gate", "failed", detail, cause="project_validate_missing")
        return True, {"exit_code": 1, "ok": False, "cause": "project_validate_missing", "error": detail}
    if report.ok:
        progress.phase("gate", "succeeded", "portão declarado pelo projeto aprovado")
        if not as_json:
            print("[+] Validação declarada pelo projeto aprovada: [HARNESS_PASS]!")
        return True, None
    detail = redact_secrets(report.combined_output)
    progress.phase("gate", "failed", redacted_head(detail[-1200:], 600), cause=failure_cause)
    if not as_json:
        print(f"[ERRO NO PORTÃO DO PROJETO]:\n{detail}", file=sys.stderr)
    return True, {
        "exit_code": report.exit_code or 1,
        "ok": False,
        "cause": failure_cause,
        "error": detail,
        "worktree": str(target_worktree),
    }


# ---------------------------------------------------------------- relatorios de entrega
def rebase_conflict_result(
    ticket: Any, target_worktree: Path, current_branch: str, current_sha: str, resume_cmd: str,
    base_branch: str, conflict_files: list[str], msg: str, progress: Any, as_json: bool, return_result: bool,
) -> Any:
    print("\n" + "=" * 68, file=sys.stderr)
    print(
        f"[ERRO DE CONFLITO NO REBASE] Conflito ao rebasear a branch {current_branch} em origin/{base_branch}.",
        file=sys.stderr,
    )
    if conflict_files:
        print("Arquivos conflitantes:", file=sys.stderr)
        for cf in conflict_files:
            print(f"  - {cf}", file=sys.stderr)
    else:
        print(f"Detalhes do erro: {msg}", file=sys.stderr)
    print(f"\nWorktree preservada para resolução manual em: {target_worktree}", file=sys.stderr)
    print("Para retomar a entrega após resolver o conflito, execute:", file=sys.stderr)
    print(f"  {resume_cmd}", file=sys.stderr)
    print("=" * 68 + "\n", file=sys.stderr)

    progress.phase(
        "gate",
        "failed",
        f"conflito no rebase em origin/main: {', '.join(conflict_files[:5]) or msg}",
        cause="rebase_conflict",
    )
    conflict_payload = {
        "exit_code": 1,
        "ok": False,
        "cause": "rebase_conflict",
        "ticket_id": ticket.id,
        "branch": current_branch,
        "current_sha": current_sha,
        "worktree": str(target_worktree),
        "conflict_files": conflict_files,
        "resume_command": resume_cmd,
    }
    if as_json and not return_result:
        print(json.dumps(conflict_payload, indent=2, ensure_ascii=False))
    return conflict_payload if return_result else 1


def delivery_failure_result(
    ticket: Any, completion_report: Any, external_project: bool, target_worktree: Path, current_branch: str,
    current_sha: str, resume_cmd: str, progress: Any, as_json: bool, return_result: bool,
) -> Any:
    error_msg = completion_report.message if completion_report else "complete_ticket failed"
    print("\n" + "=" * 68, file=sys.stderr)
    print(f"[ERRO NA ENTREGA DO TICKET {ticket.id}]:", file=sys.stderr)
    print(f"  Motivo: {error_msg}", file=sys.stderr)
    print(f"  Branch: {current_branch}", file=sys.stderr)
    print(f"  SHA: {current_sha}", file=sys.stderr)
    print(f"  Worktree preservada: {target_worktree}", file=sys.stderr)
    print("\nPara retomar a entrega deste ticket após solucionar o problema, execute:", file=sys.stderr)
    print(f"  {resume_cmd}", file=sys.stderr)
    print("=" * 68 + "\n", file=sys.stderr)

    delivery_report = (
        completion_report if external_project else completion_report.delivery
    ) if completion_report else None
    fail_cause = (
        delivery_report.action
        if delivery_report is not None and delivery_report.action == "staged_inspect_failed"
        else "delivery_failed"
    )
    progress.set_outcome(error_msg, fail_cause)
    fail_payload = {
        "exit_code": 1,
        "ok": False,
        "cause": fail_cause,
        "ticket_id": ticket.id,
        "error": error_msg,
        "branch": current_branch,
        "current_sha": current_sha,
        "worktree": str(target_worktree),
        "resume_command": resume_cmd,
    }
    if as_json and not return_result:
        print(json.dumps(fail_payload, indent=2, ensure_ascii=False))
    return fail_payload if return_result else 1


def delivery_success_result(
    ticket: Any, completion_report: Any, target_worktree: Path, current_branch: str, current_sha: str,
    attempts_log: list[dict[str, Any]], as_json: bool, return_result: bool, awaiting_deploy: bool = False,
    artifact_directory: Optional[Path] = None,
) -> Any:
    merge_artifact = artifact_directory / "merge.json" if artifact_directory else None
    deploy_artifact = artifact_directory / "deploy_report.json" if artifact_directory else None
    merge_data = json.loads(merge_artifact.read_text(encoding="utf-8")) if merge_artifact and merge_artifact.is_file() else {}
    deploy_data = json.loads(deploy_artifact.read_text(encoding="utf-8")) if deploy_artifact and deploy_artifact.is_file() else {}
    delivery = getattr(completion_report, "delivery", None) or completion_report
    pr_url = merge_data.get("pr_url") or getattr(delivery, "pr_url", None) or f"branch:{current_branch}"
    merge_sha = (
        merge_data.get("merge_sha")
        or getattr(delivery, "merge_sha", None)
        or getattr(completion_report, "commit_sha", None)
        or current_sha
    )
    deploy_status = "awaiting_deploy" if awaiting_deploy else (
        "deployed" if deploy_data.get("deployed") else "not_run"
    )
    if not as_json:
        if completion_report:
            delivered_sha = (
                getattr(completion_report, "commit_sha", None)
                or getattr(completion_report, "merge_sha", None)
                or current_sha
            )
            print(
                f"[+] Autonomia Git: Ticket {ticket.id} concluído, comitado "
                f"({delivered_sha}) e sincronizado com sucesso."
            )
        if awaiting_deploy:
            print(
                f"\n[MERGE] Ticket {ticket.id} mergeado e validado; aguardando deploy; "
                f"PR {pr_url}; SHA {merge_sha}; relatorio {deploy_artifact}. "
                "Status: merged_awaiting_deploy, nao entregue."
            )
        else:
            print(
                f"\n[SUCESSO] Ticket {ticket.id}: PR {pr_url or '-'}; SHA de merge {merge_sha or '-'}; "
                f"deploy {deploy_status}; relatorio {deploy_artifact or '-'}."
            )

    success_payload = {
        "exit_code": 0,
        "ok": True,
        "ticket_id": ticket.id,
        "branch": current_branch,
        "commit_sha": getattr(completion_report, "commit_sha", None) if completion_report else current_sha,
        "worktree": str(target_worktree),
        "status": "merged_awaiting_deploy" if awaiting_deploy else "delivered",
        "pr_url": pr_url,
        "merge_sha": merge_sha,
        "deploy_status": deploy_status,
        "deploy_report": str(deploy_artifact) if deploy_artifact and deploy_artifact.is_file() else None,
    }
    if completion_report and getattr(completion_report, "sync_result", None):
        success_payload["sync_action"] = completion_report.sync_result.action
    if attempts_log:
        success_payload["self_correction_log"] = attempts_log
    if as_json and not return_result:
        print(json.dumps(success_payload, indent=2, ensure_ascii=False))

    return success_payload if return_result else 0


# ---------------------------------------------------------------- estagios de _run
def build_dev_prompt(ticket: Any, workspace: Any) -> str:
    return (
        f"Voce e o agente de desenvolvimento autonomo da DarkFac.\n"
        f"Implemente o seguinte ticket:\n"
        f"ID: {ticket.id}\n"
        f"Titulo: {ticket.title}\n"
        f"Problema: {ticket.problem_statement}\n"
        f"Criterios de Aceite:\n" + "\n".join(f"- {c}" for c in ticket.acceptance_criteria) + "\n\n"
        f"Escreva/ajuste testes unitarios e o codigo funcional.\n"
        f"Voce trabalha numa worktree propria deste ticket (branch {workspace.branch}); "
        f"nao leia nem altere arquivos fora dela e nao faca commit, push nem merge: a fabrica entrega.\n"
    )


def continuation_prompt(workspace: Any, ledger_relative: str) -> str:
    """Aviso de continuacao quando a worktree ja tem trabalho (USR-113/USR-225); vazio caso contrario."""
    from core.git.autonomy import GitAutonomyManager
    git_mgr = GitAutonomyManager(workspace.main_root)
    existing_changes = [
        p for p in git_mgr.changed_paths(workspace.path) if p != ledger_relative
    ]
    # A preserved worktree (USR-225) may hold commits already; they count as work too.
    try:
        from core.git.autonomy import _run_git

        committed = _run_git(["diff", "--name-only", f"{workspace.base_sha}..HEAD"], cwd=workspace.path)
    except Exception as exc:  # noqa: BLE001 - the continuation hint is best effort
        logger.debug("Could not list committed changes in %s: %s", workspace.path, exc)
        committed = None
    if committed is not None and committed.returncode == 0:
        committed_paths = [p.strip() for p in committed.stdout.splitlines() if p.strip()]
        existing_changes = list(
            dict.fromkeys(existing_changes + [p for p in committed_paths if p != ledger_relative])
        )
    if not existing_changes:
        return ""
    changed_preview = ", ".join(existing_changes[:8])
    if len(existing_changes) > 8:
        changed_preview += f" (+{len(existing_changes) - 8} arquivos)"
    return (
        f"\n\n[AVISO DE CONTINUAÇÃO - WORKTREE JÁ INICIADA (USR-113)]\n"
        f"Uma tentativa anterior foi interrompida (ex.: tempo limite atingido), mas o trabalho já iniciado foi PRESERVADO nesta worktree.\n"
        f"Arquivos já criados ou alterados: {changed_preview}.\n"
        f"NÃO recomece a implementação do zero nem descarte o que foi feito. Inspecione o estado atual dos arquivos na worktree, "
        f"continue o desenvolvimento a partir de onde parou, ajuste os testes unitários e garanta que todos os critérios de aceite passem.\n"
    )


def dry_run_payload(
    ticket: Any, ticket_size: str, size_reasons: list[str], selected_harness: Any, selected_model: Any,
    development_mode: str, override_granted: bool, operating_harness: Any, route_reason: str,
) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "estimated_size": ticket_size,
        "size_reasons": size_reasons,
        "selected_harness": selected_harness,
        "selected_model": selected_model,
        "mode": development_mode,
        "override_granted": override_granted,
        "operating_harness": operating_harness,
        "route_reason": route_reason,
        "dry_run": True,
    }


def print_large_ticket_warning(ticket: Any, size_reasons: list[str]) -> None:
    box_lines = [
        "=" * 68,
        " [AVISO: TICKET ESTIMADO COMO GRANDE - RISCO DE COTA (USR-113)]",
        f" Ticket {ticket.id} possui características de alta complexidade/tamanho:",
        *[f"   - {r}" for r in size_reasons],
        " Risco: pode estourar o timeout do agente (1800s) e consumir cota elevada.",
        " Recomendação: considere quebrar este ticket em demandas menores.",
        "=" * 68,
    ]
    print("\n" + "\n".join(box_lines) + "\n", file=sys.stderr)


def delivery_command(run_script: Path, ticket_id: str, worktree: Path, args: Any) -> list[str]:
    cmd = [
        sys.executable,
        str(run_script),
        "--resume-delivery", ticket_id,
        "--worktree", str(worktree),
        "--json",
    ]
    if args.skip_validation:
        cmd.append("--skip-validation")
    if args.no_commit:
        cmd.append("--no-commit")
    if args.no_push:
        cmd.append("--no-push")
    return cmd


def delivery_child_env(progress: Any, exec_tracker: Any) -> dict[str, str]:
    # USR-370: deliberately NOT scrubbed with build_agent_env: this child is the delivery executor
    # (commit/push/PR/merge/deploy) and must keep GH/deploy credentials. Scrubbing applies to agents.
    child_env = dict(os.environ)
    # The delivery subprocess is part of THIS run, not a second launch (USR-250): it inherits the
    # line lock instead of trying to acquire (and refusing itself on) a lock its own parent holds.
    child_env = line_lock.mark_inherited(child_env)
    # The delivery subprocess joins this run for cancellation too: it must honour the request the
    # parent watches and never erase it as "stale" (it starts later than the request was written).
    child_env[local_cancel.ENV_PARENT] = progress.run_id
    if exec_tracker is not None:
        child_env[execution_registry.ENV_EXECUTION_ID] = exec_tracker.root_id  # USR-208: stages nest under it
    if progress.enabled:
        # The delivery subprocess joins this run: same run id, so the board shows one run.
        child_env[ENV_RUN_ID] = progress.run_id
    if progress.warning:
        # USR-154: the user was already warned; the subprocess must not repeat it.
        child_env[ENV_WARNED] = "1"
    return child_env


def parse_delivery_output(delivery_proc: Any, exit_code: int) -> tuple[dict[str, Any], bool]:
    """Le o JSON da entrega; devolve (completion_info, stdout_era_json)."""
    try:
        completion_info = json.loads(delivery_proc.stdout)
        if not isinstance(completion_info, dict):
            raise ValueError("delivery JSON is not an object")
        return completion_info, True
    except Exception:
        return {"ok": exit_code == 0, "error": delivery_proc.stderr or delivery_proc.stdout}, False


def final_json_payload(
    ticket: Any, ticket_size: str, cost_record: Any, selected_harness: Any, selected_model: Any,
    selected_effort: Any, agent_result: Any, workspace: Any, agent_summary: str, delivered: bool,
    completion_info: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "ok": delivered,
        "ticket_id": ticket.id,
        "estimated_size": ticket_size,
        "quota_usage": cost_record,
        "harness": selected_harness,
        "model": selected_model,
        "effort": selected_effort,
        "duration_s": agent_result.duration_s,
        "workspace": str(workspace.path),
        "branch": workspace.branch,
        "agent_summary": agent_summary,
    }
    if "commit_sha" in completion_info:
        payload["commit_sha"] = completion_info["commit_sha"]
    if "sync_action" in completion_info:
        payload["sync_action"] = completion_info["sync_action"]
    if "status" in completion_info:
        payload["status"] = completion_info["status"]
    for key in ("pr_url", "merge_sha", "deploy_status", "deploy_report"):
        if key in completion_info:
            payload[key] = completion_info[key]
    if not delivered and "error" in completion_info:
        payload["error"] = completion_info["error"]
    return payload
