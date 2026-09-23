"""Development + validate loop for the DarkFac production line (HF-27-05).

Implements the "development" and "validation" halves of the contract in
`docs/handoffs/production-line/HF-27-05.md`:

- `DevelopmentStage` walks `tickets.json` (written by planning, HF-27-04) in
  order. For each ticket without a `DarkFac-Job: <run_id>:<ticket_id>`
  commit yet, it runs a write-mode agent, then the project's `setup` +
  `validate` commands, iterating up to
  `run_caps.validate_iterations_per_ticket` times before giving up. A
  passing iteration is committed (trailer-stamped, idempotent) and pushed;
  an exhausted ticket either asks for a replan (agent reported
  `SPEC_CONFLICT:`) or fails with `cause_code="validate_exhausted"`.
- `ValidationStage` is deterministic and LLM-free: it clones the pushed
  branch tip into a throwaway directory and re-runs `setup` + `validate` +
  `build` there, to catch "works in the dirty worktree, breaks on a clean
  checkout" (e.g. a file the test needs that never got committed).

Both stages are resumable: all state lives in `.darkfac/runs/<run_id>/` on
the run's own branch (`core.line.workspace`), never in process memory, so a
retry from a different host picks up exactly where the last one left off.

Agent invocation and harness selection are injected (`run_agent_func`,
`pick_func`) so tests can supply an in-process "fake agent" that edits
files directly, per the HF-27-05 acceptance note, without spawning any real
CLI subprocess or touching the network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from pydantic import BaseModel, Field

from core.line import workspace
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import RoutingConfig, load_routing_config, pick, record_result
from core.line.workspace import RunWorkspace, WorkspaceError
from core.projects.models import ProjectCommands, ProjectDescriptor
from core.projects.registry import normalize_repo_url, resolve_commands
from core.workflow.control_contracts import StageResult

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_DIR = REPO_ROOT / "core" / "line" / "prompts"

_DEFAULT_HOST_CAPS: tuple[str, ...] = (
    "harness:claude",
    "harness:codex",
    "harness:grok",
    "harness:antigravity",
)

_LOCKFILE_CANDIDATES: tuple[str, ...] = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
)

_MISSING_TESTS_INSTRUCTION = (
    "Este projeto ainda nao tem comando de validate detectado. "
    "Crie a infraestrutura minima de testes e um teste para este ticket antes de "
    "implementar a funcionalidade."
)

_NO_EDIT_RUNS_INSTRUCTION = (
    "Escreva/ajuste testes primeiro; nao altere arquivos em .darkfac/runs."
)

_FALLBACK_DEVELOP_TEMPLATE = """\
Voce e um agente de desenvolvimento autonomo da DarkFac.

## SPEC do run
{spec}

## Ticket
ID: {ticket_id}
Titulo: {ticket_title}
Objetivo: {ticket_goal}

## Criterios de aceite
{acceptance}

## Testes a adicionar
{tests_to_add}

## Comandos do projeto
Setup: {setup_commands}
Validate: {validate_commands}

## Contexto adicional
{extra_instruction}

## Ultima falha de validate (se houver)
{last_validate_log}

## Ultima revisao (se houver)
{last_review_log}

Implemente a mudanca completa para este ticket no repositorio atual. {no_edit_runs}
"""

_FALLBACK_SPEC_CONFLICT_HINT = (
    "Se o ticket for impossivel de implementar como especificado, responda com uma linha "
    "iniciando por 'SPEC_CONFLICT:' explicando o motivo, sem tentar workarounds arriscados."
)


class TicketSpec(BaseModel):
    """One entry of `tickets.json`, written by planning (HF-27-04)."""

    id: str
    title: str
    goal: str = ""
    files_hint: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    tests_to_add: list[str] = Field(default_factory=list)
    smoke: list[str] = Field(default_factory=list)


class CommandRunResult(BaseModel):
    """Outcome of running a project's command list (setup/validate/build)."""

    ok: bool
    combined_output: str
    exit_code: int
    duration_s: float


class DevelopmentProgress(BaseModel):
    """`progress.json` — the harness that implemented the run, for the review cascade."""

    harness: Optional[str] = None
    model: Optional[str] = None
    tickets_done: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Shared helpers: prompts, command execution, log distillation
# --------------------------------------------------------------------------


def _load_prompt_template(name: str, fallback: str) -> str:
    """Read `core/line/prompts/<name>` if HF-27-04 has landed it; else use `fallback`.

    HF-27-05 does not own `core/line/prompts/` (HF-27-04 does), so this never
    writes there — it only opportunistically reads a template when present.
    """
    candidate = _PROMPTS_DIR / name
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read prompt template %s (%s); using fallback", candidate, exc)
    return fallback


class _KeepMissing(dict):
    """`format_map` mapping that leaves unknown `{name}` placeholders intact."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill_template(template: str, **values: str) -> str:
    """Tolerant `str.format`: a `prompts/*.md` with extra placeholders never raises."""
    return template.format_map(_KeepMissing(values))


# Agent failures that say nothing about the ticket: retry the stage later
# instead of burning one of the ticket's validate iterations.
_TRANSIENT_AGENT_ERRORS = frozenset({"rate_limited", "auth_expired", "not_installed"})


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def run_shell_commands(
    commands: Iterable[str], cwd: Path, *, timeout_s: int = 1800
) -> CommandRunResult:
    """Run each command in `commands` sequentially in `cwd`, stopping at the first failure.

    Commands are project-configured strings (e.g. `"npm test"`,
    `"python -m pytest -q"`); `shell=True` is required to support them as
    written, mirroring how a human or CI runner would invoke them.
    """
    combined: list[str] = []
    start = time.perf_counter()
    exit_code = 0
    for cmd in commands:
        combined.append(f"$ {cmd}")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                **_win_kwargs(),
            )
        except subprocess.TimeoutExpired:
            combined.append(f"[timeout after {timeout_s}s]")
            exit_code = 124
            break
        except OSError as exc:
            combined.append(f"[failed to launch: {exc}]")
            exit_code = 127
            break
        combined.append(proc.stdout or "")
        if proc.stderr:
            combined.append(proc.stderr)
        exit_code = proc.returncode
        if proc.returncode != 0:
            break
    duration = round(time.perf_counter() - start, 3)
    return CommandRunResult(
        ok=exit_code == 0, combined_output="\n".join(combined), exit_code=exit_code, duration_s=duration
    )


def distill_log(combined_output: str, *, max_lines: int = 150) -> str:
    """Best-effort log distillation: last `max_lines` plus every FAIL|Error line.

    Mirrors the HF-27-05 fallback path for when
    `core.harness.test_subagent.TestSubagentEngine.parse_output` does not
    produce a usable report (e.g. output is not pytest-shaped).
    """
    lines = combined_output.splitlines()
    tail = lines[-max_lines:]
    flagged = [ln for ln in lines if ("FAIL" in ln or "Error" in ln) and ln not in tail]
    parts: list[str] = []
    if flagged:
        parts.append("## Linhas com FAIL|Error\n" + "\n".join(flagged))
    parts.append(f"## Ultimas {len(tail)} linhas\n" + "\n".join(tail))
    return "\n\n".join(parts)


def _distill_with_test_subagent(result: CommandRunResult) -> str:
    """Try `TestSubagentEngine.parse_output`; fall back to `distill_log` on any issue."""
    try:
        from core.harness.test_subagent import TestSubagentEngine

        engine = TestSubagentEngine()
        report = engine.parse_output(result.combined_output, result.exit_code, result.duration_s)
        return (
            f"verdict={report.verdict} ({report.concise_summary})\n\n{report.agent_feedback}"
        )
    except Exception as exc:  # pragma: no cover - defensive, distillation must never crash the stage
        logger.debug("test_subagent distillation unavailable (%s); using raw fallback", exc)
        return distill_log(result.combined_output)


def _lockfile_hash(repo_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in _LOCKFILE_CANDIDATES:
        path = repo_dir / name
        if path.is_file():
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()


def _setup_cache_path(ws: RunWorkspace) -> Path:
    """Host-local cache marker, deliberately kept *outside* the git worktree.

    `.darkfac/runs/<run_id>/` is versioned branch state per the common line
    contract (`docs/handoffs/production-line/INDEX.md`); this file is a pure
    performance optimization for this host's checkout and must never be
    picked up by `commit()`'s `git add -A` or become part of the run's
    resumable state.
    """
    return ws.path.parent / f".{ws.run_id}.setup_hash"


def ensure_setup(ws: RunWorkspace, project: ProjectDescriptor, commands: ProjectCommands) -> CommandRunResult:
    """Run `commands.setup` once per worktree, cached by a hash of the lockfiles present."""
    if not commands.setup:
        return CommandRunResult(ok=True, combined_output="", exit_code=0, duration_s=0.0)
    cache_path = _setup_cache_path(ws)
    current_hash = _lockfile_hash(ws.path)
    if cache_path.is_file():
        try:
            cached_hash = cache_path.read_text(encoding="utf-8").strip()
        except OSError:
            cached_hash = ""
        if cached_hash == current_hash:
            return CommandRunResult(ok=True, combined_output="[setup cache hit]", exit_code=0, duration_s=0.0)
    result = run_shell_commands(commands.setup, ws.path)
    if result.ok:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(current_hash, encoding="utf-8")
    return result


# --------------------------------------------------------------------------
# tickets.json / progress.json I/O
# --------------------------------------------------------------------------


def load_tickets(ws: RunWorkspace) -> list[TicketSpec]:
    """Load `tickets.json` written by planning; empty list if absent or empty."""
    path = workspace.context_dir(ws) / "tickets.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read tickets.json at %s (%s)", path, exc)
        return []
    if not isinstance(raw, list):
        return []
    return [TicketSpec.model_validate(item) for item in raw]


def _read_progress(ws: RunWorkspace) -> DevelopmentProgress:
    path = workspace.context_dir(ws) / "progress.json"
    if not path.is_file():
        return DevelopmentProgress()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DevelopmentProgress()
    return DevelopmentProgress.model_validate(raw)


def _write_progress(ws: RunWorkspace, progress: DevelopmentProgress) -> None:
    workspace.write_context(ws, "progress.json", progress.model_dump_json(indent=2))


# --------------------------------------------------------------------------
# DevelopmentStage
# --------------------------------------------------------------------------


class DevelopmentStage:
    """Runs the write-mode agent + validate loop for every pending ticket."""

    def __init__(
        self,
        *,
        run_agent_func: Callable[[AgentRequest], AgentResult] = run_agent,
        pick_func: Callable[..., Optional[tuple[str, Optional[str]]]] = pick,
        host_caps: Iterable[str] = _DEFAULT_HOST_CAPS,
        routing_config: Optional[RoutingConfig] = None,
        agent_timeout_s: int = 1800,
        command_timeout_s: int = 1800,
    ) -> None:
        self.run_agent_func = run_agent_func
        self.pick_func = pick_func
        self.host_caps = list(host_caps)
        self.routing_config = routing_config or load_routing_config()
        self.agent_timeout_s = agent_timeout_s
        self.command_timeout_s = command_timeout_s

    # -- prompt building ---------------------------------------------------

    def _build_prompt(
        self,
        ticket: TicketSpec,
        commands: ProjectCommands,
        *,
        extra_instruction: str,
        last_validate_log: Optional[str],
        last_review_log: Optional[str],
        spec_text: str = "",
    ) -> str:
        template = _load_prompt_template("develop.md", _FALLBACK_DEVELOP_TEMPLATE)
        return fill_template(
            template,
            spec=spec_text or "(SPEC.md ausente)",
            ticket_id=ticket.id,
            ticket_title=ticket.title,
            ticket_goal=ticket.goal or "(nao informado)",
            acceptance="\n".join(f"- {a}" for a in ticket.acceptance) or "(nenhum informado)",
            tests_to_add="\n".join(f"- {t}" for t in ticket.tests_to_add) or "(nenhum informado)",
            setup_commands="; ".join(commands.setup) or "(nenhum)",
            validate_commands="; ".join(commands.validate_cmds) or "(nenhum)",
            extra_instruction=extra_instruction or "(nenhum)",
            last_validate_log=last_validate_log or "(nenhuma iteracao anterior)",
            last_review_log=last_review_log or "(nenhuma revisao anterior)",
            no_edit_runs=f"{_NO_EDIT_RUNS_INSTRUCTION} {_FALLBACK_SPEC_CONFLICT_HINT}",
        )

    # -- single ticket -------------------------------------------------

    def _develop_ticket(
        self,
        ws: RunWorkspace,
        run_id: str,
        project: ProjectDescriptor,
        ticket: TicketSpec,
        ticket_index: int,
        progress: DevelopmentProgress,
        *,
        initial_feedback: Optional[str] = None,
    ) -> StageResult:
        job_key = f"{run_id}:{ticket.id}"
        max_iterations = self.routing_config.run_caps.validate_iterations_per_ticket
        last_validate_log: Optional[str] = initial_feedback
        last_review_log = _latest_review_log(ws)
        spec_text = _read_context_text(ws, "SPEC.md")

        for iteration in range(1, max_iterations + 1):
            commands = resolve_commands(project, ws.path)
            extra_instruction = ""
            if ticket_index == 0 and not commands.validate_cmds:
                extra_instruction = _MISSING_TESTS_INSTRUCTION

            prompt = self._build_prompt(
                ticket,
                commands,
                extra_instruction=extra_instruction,
                last_validate_log=last_validate_log,
                last_review_log=last_review_log,
                spec_text=spec_text,
            )

            route = self.pick_func(
                "development", self.host_caps, config=self.routing_config
            )
            if route is None:
                return StageResult(
                    outcome="waiting_human", cause_code="no_route_available", output_refs=[]
                )
            harness, model = route

            agent_result = self.run_agent_func(
                AgentRequest(
                    prompt=prompt,
                    cwd=ws.path,
                    mode="write",
                    harness=harness,
                    model=model,
                    timeout_s=self.agent_timeout_s,
                )
            )
            record_result(agent_result, config=self.routing_config)

            if not agent_result.ok and agent_result.error_kind in _TRANSIENT_AGENT_ERRORS:
                return StageResult(
                    outcome="retry", cause_code=f"agent_{agent_result.error_kind}", output_refs=[]
                )
            if not agent_result.ok:
                last_validate_log = (
                    f"Falha ao invocar agente ({agent_result.error_kind}): {agent_result.text}"
                )
                _write_validate_log(ws, ticket.id, iteration, last_validate_log)
                continue

            # Re-detect after the agent ran: it may have just created the
            # minimal test infrastructure the extra instruction asked for.
            commands = resolve_commands(project, ws.path)
            setup_result = ensure_setup(ws, project, commands)
            if not setup_result.ok:
                last_validate_log = "Falha no setup:\n" + _distill_with_test_subagent(setup_result)
                _write_validate_log(ws, ticket.id, iteration, last_validate_log)
                continue

            if not commands.validate_cmds:
                # Still nothing to validate against, even after the extra
                # instruction (ticket 0) or normally (later tickets should
                # have inherited test infra by now). Treat as a failed
                # iteration rather than a silent pass, so the loop keeps
                # nudging the agent instead of committing unverified work.
                validate_result = CommandRunResult(
                    ok=False,
                    combined_output="Nenhum comando de validate foi detectado no projeto.",
                    exit_code=1,
                    duration_s=0.0,
                )
            else:
                validate_result = run_shell_commands(
                    commands.validate_cmds, ws.path, timeout_s=self.command_timeout_s
                )

            distilled = _distill_with_test_subagent(validate_result)
            _write_validate_log(ws, ticket.id, iteration, distilled)

            if validate_result.ok:
                # progress.json goes into the same commit: review (possibly on
                # another host) reads the implementing harness from the branch,
                # and it keeps a fix-up commit non-empty so its trailer lands.
                progress.harness = harness
                progress.model = model
                if ticket.id not in progress.tickets_done:
                    progress.tickets_done.append(ticket.id)
                _write_progress(ws, progress)
                sha = workspace.commit(ws, f"feat: {ticket.title}", job_key=job_key)
                workspace.push(ws)
                return StageResult(outcome="success", output_refs=[sha])

            last_validate_log = distilled
            if "SPEC_CONFLICT:" in agent_result.text:
                return StageResult(outcome="replan", cause_code="spec_conflict", output_refs=[])

        return StageResult(outcome="failed", cause_code="validate_exhausted", output_refs=[])

    # -- full run --------------------------------------------------------

    def run(self, project: ProjectDescriptor, run_id: str) -> StageResult:
        """Develop every ticket in `tickets.json` still missing its job commit.

        Resumable: a ticket already carrying a `DarkFac-Job: <run_id>:<id>`
        commit is skipped outright, so a retried run picks up where the
        previous one stopped.
        """
        ws = workspace.checkout(project, run_id)
        tickets = load_tickets(ws)
        if not tickets:
            return StageResult(outcome="failed", cause_code="no_tickets", output_refs=[])

        progress = _read_progress(ws)
        last_sha: Optional[str] = None
        for index, ticket in enumerate(tickets):
            job_key = f"{run_id}:{ticket.id}"
            existing_sha = workspace.find_commit_by_job(ws, job_key)
            if existing_sha:
                last_sha = existing_sha
                continue

            result = self._develop_ticket(ws, run_id, project, ticket, index, progress)
            if result.outcome != "success":
                return result
            last_sha = result.output_refs[0]

        # Every ticket is committed. If review or clean validation sent the
        # run back here, address that feedback in one fix-up pass; otherwise
        # the retry would be a no-op and the loop would never converge.
        fixup = _pending_fixup(ws, run_id)
        if fixup is not None:
            fix_ticket, feedback = fixup
            result = self._develop_ticket(
                ws, run_id, project, fix_ticket, len(tickets), progress, initial_feedback=feedback
            )
            if result.outcome != "success":
                return result
            last_sha = result.output_refs[0]

        return StageResult(outcome="success", output_refs=[last_sha] if last_sha else [])


def _read_context_text(ws: RunWorkspace, name: str) -> str:
    path = workspace.context_dir(ws) / name
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_context_json(ws: RunWorkspace, name: str) -> dict[str, Any]:
    try:
        data = json.loads(_read_context_text(ws, name) or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _pending_fixup(ws: RunWorkspace, run_id: str) -> Optional[tuple[TicketSpec, str]]:
    """Unaddressed feedback from review (`changes_required`) or a red clean validation.

    Returns a synthetic fix-up ticket whose job key (`<run_id>:fix-...`) is
    unique per feedback item, so each item is addressed exactly once.
    """
    rounds = _read_context_json(ws, "review_state.json").get("rounds")
    if isinstance(rounds, list) and rounds and isinstance(rounds[-1], dict):
        last = rounds[-1]
        round_num = last.get("round")
        if last.get("verdict") == "changes_required" and round_num is not None:
            ticket = TicketSpec(
                id=f"fix-review-{round_num}",
                title=f"address review round {round_num}",
                goal="Corrigir todos os itens bloqueantes da ultima revisao.",
            )
            if not workspace.find_commit_by_job(ws, f"{run_id}:{ticket.id}"):
                return ticket, _read_context_text(ws, f"review-{round_num}.md")

    validation = _read_context_json(ws, "validation.json")
    commands = validation.get("commands")
    sha = str(validation.get("sha") or "")[:12]
    if isinstance(commands, dict) and sha:
        failed = {
            name: entry
            for name, entry in commands.items()
            if isinstance(entry, dict) and entry.get("ran") and not entry.get("ok", True)
        }
        if failed:
            ticket = TicketSpec(
                id=f"fix-validation-{sha}",
                title=f"fix clean-checkout validation at {sha}",
                goal="A validacao em clone limpo falhou; corrija (ex.: arquivo nao commitado).",
            )
            if not workspace.find_commit_by_job(ws, f"{run_id}:{ticket.id}"):
                logs = "\n\n".join(f"## {name}\n{entry.get('log', '')}" for name, entry in failed.items())
                return ticket, f"Validacao em clone limpo falhou:\n{logs}"
    return None


def _write_validate_log(ws: RunWorkspace, ticket_id: str, iteration: int, content: str) -> None:
    workspace.write_context(ws, f"validate-{ticket_id}-{iteration}.log.md", content)


def _latest_review_log(ws: RunWorkspace) -> Optional[str]:
    context_dir = workspace.context_dir(ws)
    if not context_dir.is_dir():
        return None
    review_files = sorted(context_dir.glob("review-*.md"))
    if not review_files:
        return None
    try:
        return review_files[-1].read_text(encoding="utf-8")
    except OSError:
        return None


# --------------------------------------------------------------------------
# ValidationStage
# --------------------------------------------------------------------------


class ValidationStage:
    """Deterministic, LLM-free clean-checkout validation of the pushed branch tip."""

    def __init__(self, *, command_timeout_s: int = 1800, clone_timeout_s: int = 900) -> None:
        self.command_timeout_s = command_timeout_s
        self.clone_timeout_s = clone_timeout_s

    def _clean_clone(self, project: ProjectDescriptor, run_id: str, dest: Path) -> str:
        """Clone the tip of `df/<run_id>` into `dest`; returns the checked-out SHA."""
        if not project.repo_url:
            raise WorkspaceError(f"project '{project.id}' has no repo_url configured")
        repo_url = normalize_repo_url(project.repo_url)
        branch = f"df/{run_id}"
        token = workspace._github_token_for(repo_url)
        argv = [
            "git",
            *workspace._auth_args(token),
            "clone",
            "--branch",
            branch,
            "--single-branch",
            "--depth",
            "1",
            repo_url,
            str(dest),
        ]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.clone_timeout_s,
            **_win_kwargs(),
        )
        if proc.returncode != 0:
            stderr = workspace._sanitize((proc.stderr or "").strip(), token)
            raise WorkspaceError(f"clean clone of {branch} failed: {stderr}")
        rev_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_kwargs(),
        )
        return (rev_proc.stdout or "").strip()

    def run(self, project: ProjectDescriptor, run_id: str) -> StageResult:
        ws = workspace.checkout(project, run_id)
        tmp_dir = Path(tempfile.mkdtemp(prefix="darkfac-validate-"))
        try:
            sha = self._clean_clone(project, run_id, tmp_dir)
        except WorkspaceError:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return StageResult(outcome="retry", cause_code="clean_clone_failed", output_refs=[])

        try:
            commands = resolve_commands(project, tmp_dir)
            report: dict[str, Any] = {"sha": sha, "commands": {}, "started_at": time.time()}
            all_ok = True

            for stage_name, cmd_list in (
                ("setup", commands.setup),
                ("validate", commands.validate_cmds),
                ("build", commands.build),
            ):
                if not cmd_list:
                    report["commands"][stage_name] = {"ran": False}
                    continue
                result = run_shell_commands(cmd_list, tmp_dir, timeout_s=self.command_timeout_s)
                report["commands"][stage_name] = {
                    "ran": True,
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "duration_s": result.duration_s,
                }
                if not result.ok:
                    report["commands"][stage_name]["log"] = _distill_with_test_subagent(result)
                    all_ok = False
                    break

            workspace.write_context(ws, "validation.json", json.dumps(report, indent=2, ensure_ascii=False))
            job_key = f"{run_id}:validation:{sha}"
            commit_sha = workspace.commit(ws, "chore: clean-checkout validation", job_key=job_key)
            workspace.push(ws)

            if all_ok:
                return StageResult(outcome="success", output_refs=[commit_sha])
            return StageResult(outcome="retry", cause_code="clean_validate_failed", output_refs=[commit_sha])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
