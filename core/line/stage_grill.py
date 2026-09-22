"""Single-round Grill stage for the DarkFac production line (HF-27-04).

Implements section "Grill" of `docs/handoffs/production-line/HF-27-04.md`
and the owner decision in `docs/PRODUCTION_LINE_PLAN_2026-09-22.md` (section
2.4): **one round**, **one Telegram message**, resolved in **12h** with the
recommended answer, except `secret`/`account` questions which never get a
default.

Flow (`run_grill`):

1. `workspace.checkout(project, run_id)` and write `DEMAND.md`.
2. If a finished grill commit already exists for this run (`DarkFac-Job:
   <run_id>:grill`), return `success` immediately (idempotent replay).
3. If no grill has run yet for this run: invoke the agent in **read** mode
   with `prompts/grill.md`. Parse and validate its JSON reply, cap it at 5
   questions, and split them into `technical` (resolved immediately with the
   recommended option, recorded as a premise) and blocking
   (`business`/`intent`/`secret`/`account`).
   - No blocking questions: write the final `GRILL.md`, commit (job key
     `<run_id>:grill`), push and return `success`.
   - Blocking questions: write a partial `GRILL.md` plus a
     `GRILL_PENDING.json` state file, commit (job key
     `<run_id>:grill:pending`, distinct from the final key so idempotency
     does not short-circuit before the questions are resolved), push, send
     **one** Telegram message bundling every blocking question, and return
     `waiting_human` with a `deadline` evidence ref.
4. If a pending grill state already exists (this is the reconciler calling
   again after `submit_grill_answers()` or after the deadline): finalize
   every question that has an answer or whose deadline has passed
   (`recommended`, tagged `default_after_timeout`) — except `secret`/
   `account` questions, which stay unresolved (a `HumanRequest` is
   HF-27-08's job) and never receive a default. If at least one non-exempt
   question is still unanswered and the deadline has not passed, return
   `waiting_human` again without committing. Otherwise finalize `GRILL.md`,
   clear the pending state, commit (job key `<run_id>:grill`), push, return
   `success`.

`submit_grill_answers()` is the entry point HF-27-08's Telegram callback
handler (`cb:grill:<run_id>#<question_id>:<choice>`) is expected to call; it
only persists the answer into the pending state on disk so the next
`run_grill()` call can finalize it — it never itself commits/pushes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from core.line import workspace
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import RoutingConfig, pick, record_result
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult

logger = logging.getLogger(__name__)

STAGE = "grill"
GRILL_DEADLINE_HOURS = 12
MAX_QUESTIONS = 5
_NO_DEFAULT_KINDS = frozenset({"secret", "account"})
_BLOCKING_KINDS = frozenset({"business", "intent", "secret", "account"})

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_DEMAND_FILE = "DEMAND.md"
GRILL_FILE = "GRILL.md"
_GRILL_FILE = GRILL_FILE  # internal alias
_PENDING_FILE = "GRILL_PENDING.json"

TelegramSender = Callable[[str, list[list[dict[str, str]]]], bool]


# --------------------------------------------------------------------------
# Agent-facing contracts
# --------------------------------------------------------------------------


class GrillQuestion(BaseModel):
    """One question the grill agent wants the owner (or itself) to resolve."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["business", "intent", "secret", "account", "technical"]
    options: list[str] = Field(default_factory=list)
    recommended: Optional[str] = None
    why: Optional[str] = None


class GrillPlan(BaseModel):
    """Parsed JSON reply from `prompts/grill.md`."""

    questions: list[GrillQuestion] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    is_product_scale: bool = False


class _PendingQuestion(BaseModel):
    id: str
    text: str
    kind: str
    options: list[str] = Field(default_factory=list)
    recommended: Optional[str] = None
    why: Optional[str] = None


class PendingGrill(BaseModel):
    """State persisted under `.darkfac/runs/<run_id>/GRILL_PENDING.json`."""

    assumptions: list[str] = Field(default_factory=list)
    technical: list[_PendingQuestion] = Field(default_factory=list)
    questions: list[_PendingQuestion] = Field(default_factory=list)
    deadline: datetime
    answers: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _job_key(run_id: str) -> str:
    return f"{run_id}:grill"


def _pending_job_key(run_id: str) -> str:
    return f"{run_id}:grill:pending"


def _now(now: Optional[datetime]) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def render_prompt(template: str, **values: str) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def read_lessons(repo_path: Path, max_lines: int = 20) -> str:
    """Last `max_lines` of `.darkfac/LESSONS.md` in the target repo, or ''."""
    lessons_file = repo_path / ".darkfac" / "LESSONS.md"
    if not lessons_file.is_file():
        return ""
    try:
        text = lessons_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in agent output")
    return stripped[start : end + 1]


def parse_grill_plan(text: str) -> GrillPlan:
    """Parse and validate an agent's grill JSON reply. Raises on malformed input."""
    candidate = _extract_json_object(text)
    data = json.loads(candidate)
    return GrillPlan.model_validate(data)


def run_read_agent(
    stage: str,
    prompt: str,
    cwd: Path,
    *,
    host_caps: Iterable[str],
    routing_config: Optional[RoutingConfig],
    max_attempts: int = 4,
) -> AgentResult:
    """Run one read-mode agent call, trying the stage's cascade until one answers.

    Shared by grill and planning: walks `routing.pick()` excluding harnesses
    that already failed in this call, recording cooldowns for rate-limited
    accounts. Returns the last (failing) `AgentResult` if nothing worked.
    """
    excluded: set[tuple[str, Optional[str]]] = set()
    last_result: Optional[AgentResult] = None
    for _ in range(max_attempts):
        choice = pick(
            stage,
            host_caps,
            exclude=excluded,
            config=routing_config,
        )
        if choice is None:
            break
        harness, model = choice
        req = AgentRequest(prompt=prompt, cwd=cwd, mode="read", harness=harness, model=model)
        result = run_agent(req)
        last_result = result
        if result.ok:
            return result
        record_result(result, config=routing_config)
        excluded.add((harness, model))
    if last_result is not None:
        return last_result
    return AgentResult(
        ok=False, text="no agent route available", harness="none", duration_s=0.0, error_kind="not_installed"
    )


def _render_grill_markdown(
    *,
    assumptions: list[str],
    technical: list[_PendingQuestion],
    resolved: list[dict[str, Any]],
    pending: list[_PendingQuestion],
) -> str:
    lines = ["# GRILL", ""]
    lines.append("## Premissas")
    if assumptions or technical:
        for item in assumptions:
            lines.append(f"- {item}")
        for question in technical:
            lines.append(
                f"- (technical) {question.text} -> **{question.recommended}** "
                f"({question.why or 'recomendacao automatica'})"
            )
    else:
        lines.append("- (nenhuma)")
    lines.append("")
    lines.append("## Decisoes do owner")
    if resolved:
        for item in resolved:
            lines.append(
                f"- [{item['kind']}] {item['text']} -> **{item['chosen']}** ({item['note']})"
            )
    else:
        lines.append("- (nenhuma pergunta bloqueante)")
    if pending:
        lines.append("")
        lines.append("## Aguardando decisao do owner")
        for question in pending:
            lines.append(f"- [{question.kind}] {question.text} (id={question.id})")
    lines.append("")
    return "\n".join(lines)


def _pending_path(ws: workspace.RunWorkspace) -> Path:
    return workspace.context_dir(ws) / _PENDING_FILE


def _load_pending(ws: workspace.RunWorkspace) -> Optional[PendingGrill]:
    path = _pending_path(ws)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PendingGrill.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Failed to load pending grill state %s: %s", path, exc)
        return None


def _save_pending(ws: workspace.RunWorkspace, pending: PendingGrill) -> None:
    workspace.write_context(ws, _PENDING_FILE, pending.model_dump_json(indent=2))


def _clear_pending(ws: workspace.RunWorkspace) -> None:
    path = _pending_path(ws)
    if path.is_file():
        path.unlink()


# --------------------------------------------------------------------------
# submit_grill_answers — entry point for the (future, HF-27-08) Telegram callback
# --------------------------------------------------------------------------


def submit_grill_answers(project: ProjectDescriptor, run_id: str, answers: dict[str, str]) -> bool:
    """Persist owner answers for a pending grill; the next `run_grill()` finalizes them.

    Returns `True` if a pending grill existed for `run_id` and the answers
    were recorded, `False` otherwise (nothing to do — no pending grill, or
    the grill already finished).
    """
    ws = workspace.checkout(project, run_id)
    pending = _load_pending(ws)
    if pending is None:
        return False
    pending.answers.update(answers)
    _save_pending(ws, pending)
    workspace.commit(ws, "grill: owner answers received", _pending_job_key(run_id))
    workspace.push(ws)
    return True


# --------------------------------------------------------------------------
# Telegram (best-effort default sender; always injectable for tests)
# --------------------------------------------------------------------------


def default_telegram_sender() -> Optional[TelegramSender]:
    """Best-effort owner-bot sender; returns `None` (no-op) if unconfigured."""
    try:
        from core.integrations.telegram import TelegramGateway, load_telegram_config
    except Exception:  # pragma: no cover - defensive import guard
        return None
    try:
        config = load_telegram_config(role="owner")
    except Exception:  # pragma: no cover - defensive
        return None
    if not config.bot_token or not config.authorized_chat_ids:
        return None
    gateway = TelegramGateway(config=config)
    chat_id = config.authorized_chat_ids[0]

    def _send(text: str, buttons: list[list[dict[str, str]]]) -> bool:
        try:
            return gateway.send_message(chat_id, text, buttons)
        except Exception as exc:  # pragma: no cover - never let notification break the stage
            logger.warning("Telegram grill notification failed: %s", exc)
            return False

    return _send


def _build_message(run_id: str, questions: list[_PendingQuestion]) -> tuple[str, list[list[dict[str, str]]]]:
    lines = [f"Grill pendente para o run {run_id} (uma rodada, prazo de {GRILL_DEADLINE_HOURS}h):", ""]
    buttons: list[list[dict[str, str]]] = []
    for question in questions:
        lines.append(f"- [{question.kind}] {question.text}")
        if question.recommended:
            lines.append(f"  recomendado: {question.recommended}")
        row = [
            {"text": option, "callback_data": f"cb:grill:{run_id}#{question.id}:{option}"}
            for option in question.options
        ]
        if row:
            buttons.append(row)
    return "\n".join(lines), buttons


# --------------------------------------------------------------------------
# run_grill
# --------------------------------------------------------------------------


def run_grill(
    project: ProjectDescriptor,
    run_id: str,
    demand_text: str,
    *,
    channel: str = "line",
    host_caps: Iterable[str] = ("harness:claude", "harness:codex"),
    routing_config: Optional[RoutingConfig] = None,
    send_message: Optional[TelegramSender] = None,
    now: Optional[datetime] = None,
) -> StageResult:
    """Run (or reconcile) the single-round grill for `run_id`.

    Idempotent: a finished grill is never re-run; a pending grill is
    resolved (fully or partially) rather than restarted.
    """
    current_time = _now(now)
    ws = workspace.checkout(project, run_id)

    existing = workspace.find_commit_by_job(ws, _job_key(run_id))
    if existing:
        return StageResult(outcome="success", output_refs=[existing])

    pending = _load_pending(ws)
    if pending is not None:
        return _reconcile_pending(ws, run_id, pending, current_time)

    workspace.write_context(ws, _DEMAND_FILE, _render_demand_markdown(project, channel, demand_text))

    prompt = render_prompt(
        load_prompt("grill.md"),
        demand=demand_text,
        grill="(nenhuma premissa anterior)",
        lessons=read_lessons(ws.path) or "(nenhuma)",
    )
    result = run_read_agent(STAGE, prompt, ws.path, host_caps=host_caps, routing_config=routing_config)
    if not result.ok:
        return StageResult(outcome="retry", cause_code=result.error_kind or "grill_agent_failed")

    try:
        plan = parse_grill_plan(result.text)
    except (ValueError, json.JSONDecodeError, ValidationError):
        return StageResult(outcome="failed", cause_code="grill_invalid_json")

    questions = plan.questions[:MAX_QUESTIONS]
    technical = [_PendingQuestion(**q.model_dump()) for q in questions if q.kind == "technical"]
    blocking = [_PendingQuestion(**q.model_dump()) for q in questions if q.kind in _BLOCKING_KINDS]

    if not blocking:
        markdown = _render_grill_markdown(
            assumptions=plan.assumptions, technical=technical, resolved=[], pending=[]
        )
        workspace.write_context(ws, GRILL_FILE, markdown)
        sha = workspace.commit(ws, "grill: no blocking questions", _job_key(run_id))
        workspace.push(ws)
        return StageResult(outcome="success", output_refs=[sha])

    deadline = current_time + timedelta(hours=GRILL_DEADLINE_HOURS)
    pending_state = PendingGrill(
        assumptions=plan.assumptions, technical=technical, questions=blocking, deadline=deadline
    )
    markdown = _render_grill_markdown(
        assumptions=plan.assumptions, technical=technical, resolved=[], pending=blocking
    )
    workspace.write_context(ws, GRILL_FILE, markdown)
    _save_pending(ws, pending_state)
    sha = workspace.commit(ws, "grill: awaiting owner decisions", _pending_job_key(run_id))
    workspace.push(ws)

    sender = send_message if send_message is not None else default_telegram_sender()
    if sender is not None:
        text, buttons = _build_message(run_id, blocking)
        try:
            sender(text, buttons)
        except Exception as exc:  # pragma: no cover - notification must never break the stage
            logger.warning("Failed to send grill Telegram message for run %s: %s", run_id, exc)

    return StageResult(
        outcome="waiting_human",
        output_refs=[sha],
        evidence_refs=[f"grill_deadline:{deadline.isoformat()}"],
    )


def _reconcile_pending(
    ws: workspace.RunWorkspace, run_id: str, pending: PendingGrill, current_time: datetime
) -> StageResult:
    deadline = pending.deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    deadline_passed = current_time >= deadline

    resolvable: list[dict[str, Any]] = []
    still_pending: list[_PendingQuestion] = []
    for question in pending.questions:
        answer = pending.answers.get(question.id)
        if answer is not None:
            resolvable.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": answer, "note": "respondida pelo owner"}
            )
        elif deadline_passed:
            if question.kind in _NO_DEFAULT_KINDS:
                still_pending.append(question)
            else:
                resolvable.append(
                    {
                        "id": question.id,
                        "text": question.text,
                        "kind": question.kind,
                        "chosen": question.recommended,
                        "note": "default_after_timeout",
                    }
                )
        else:
            still_pending.append(question)

    ready_to_finalize = deadline_passed or all(q.id in pending.answers for q in pending.questions)

    if not ready_to_finalize:
        return StageResult(
            outcome="waiting_human",
            evidence_refs=[f"grill_deadline:{deadline.isoformat()}"],
        )

    markdown = _render_grill_markdown(
        assumptions=pending.assumptions,
        technical=pending.technical,
        resolved=resolvable,
        pending=still_pending,
    )
    workspace.write_context(ws, GRILL_FILE, markdown)
    _clear_pending(ws)
    sha = workspace.commit(ws, "grill: resolved", _job_key(run_id))
    workspace.push(ws)

    evidence = []
    if still_pending:
        evidence = [f"blocked_no_default:{q.id}" for q in still_pending]
    return StageResult(outcome="success", output_refs=[sha], evidence_refs=evidence)


def _render_demand_markdown(project: ProjectDescriptor, channel: str, demand_text: str) -> str:
    return (
        f"# DEMAND\n\n"
        f"- projeto: {project.id}\n"
        f"- canal: {channel}\n\n"
        f"## Texto original\n\n{demand_text}\n"
    )
