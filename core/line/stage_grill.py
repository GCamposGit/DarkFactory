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

import html
import json
import logging
import os
import re
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
# Canary demands must never wait for a human: their intent/business questions are resolved
# immediately with the recommended answer (tag `auto_canary`). A run is a canary run when its
# payload says `grill_policy: "auto"` (set by core.line.canary) or when it belongs to one of these
# projects (covers runs accepted before the flag existed, e.g. an already-pending grill).
AUTO_GRILL_POLICY = "auto"
AUTO_GRILL_PROJECT_IDS = frozenset({"darkfac-canary"})
AUTO_CANARY_NOTE = "auto_canary"

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
    notified: bool = False
    # Canary auto-resolutions decided at grill time (kept so a later reconcile still renders them).
    auto_resolved: list[dict[str, Any]] = Field(default_factory=list)


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


_PROMPT_TOKEN = re.compile(r"\{\{|\}\}|\{(\w+)\}")


def render_prompt(template: str, **values: str) -> str:
    """Fill `{name}` placeholders and unescape `{{`/`}}` in a single pass.

    Single pass so braces inside substituted values (e.g. JSON in a demand)
    are never re-interpreted; unknown `{name}` tokens are left untouched.
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        return values[name] if name in values else token

    return _PROMPT_TOKEN.sub(_replace, template)


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


# Agent failures that only a human/ops action can fix (log a harness in, set a
# token). Retrying immediately would just burn iterations, so the retry carries
# a `not_before=<iso>` token (successors.py D-c) that also bounds the wait by
# the run's wall clock instead of the same-stage count cap.
_WAIT_FOR_AUTH_KINDS = frozenset({"no_authenticated_harness", "auth_expired"})
AUTH_RETRY_BACKOFF_MINUTES = 10


def retry_for_agent_failure(
    result: AgentResult, fallback_cause: str, *, now: Optional[datetime] = None
) -> StageResult:
    """`retry` StageResult for a failed agent call, with a short cause code.

    `<error_kind>` normally; for auth-type failures
    `<error_kind> not_before=<iso>` (<= 64 chars, the persisted column width).
    """
    kind = result.error_kind or fallback_cause
    if kind in _WAIT_FOR_AUTH_KINDS:
        when = (now or datetime.now(timezone.utc)) + timedelta(minutes=AUTH_RETRY_BACKOFF_MINUTES)
        return StageResult(
            outcome="retry", cause_code=f"{kind} not_before={when.replace(microsecond=0).isoformat()}"
        )
    return StageResult(outcome="retry", cause_code=kind)


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
    # pick() found nothing routable at all: no `harness:*` cap survived the
    # auth probe on this worker AND OpenRouter is unavailable/uncredited for
    # the stage. Clear, short, retryable cause code (never a pydantic crash).
    return AgentResult(
        ok=False,
        text="no authenticated harness available for this stage",
        harness="none",
        duration_s=0.0,
        error_kind="no_authenticated_harness",
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


def is_auto_grill(project: ProjectDescriptor, payload: Optional[dict[str, Any]] = None) -> bool:
    """True when this run's grill must never wait for a human (canary demands)."""
    if (payload or {}).get("grill_policy") == AUTO_GRILL_POLICY:
        return True
    return project.id in AUTO_GRILL_PROJECT_IDS


def _auto_answer(question: _PendingQuestion) -> Optional[str]:
    """The answer a canary auto-resolves `question` with, or None if it must keep blocking."""
    if question.kind in _NO_DEFAULT_KINDS:
        return None
    if question.recommended:
        return question.recommended
    return question.options[0] if question.options else None


def _split_auto_resolvable(
    questions: list[_PendingQuestion],
) -> tuple[list[dict[str, Any]], list[_PendingQuestion]]:
    """(auto-resolved decisions, questions that still need a human)."""
    resolved: list[dict[str, Any]] = []
    remaining: list[_PendingQuestion] = []
    for question in questions:
        answer = _auto_answer(question)
        if answer is None:
            remaining.append(question)
        else:
            resolved.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": answer, "note": AUTO_CANARY_NOTE}
            )
    return resolved, remaining


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
    by_id = {q.id: q for q in pending.questions}
    for question_id, choice in answers.items():
        question = by_id.get(question_id)
        pending.answers[question_id] = (
            resolve_callback_choice(question, choice) if question is not None else choice
        )
    _save_pending(ws, pending)
    workspace.commit(ws, "grill: owner answers received", _pending_job_key(run_id))
    workspace.push(ws)
    return True


# --------------------------------------------------------------------------
# Telegram (best-effort default sender; always injectable for tests)
# --------------------------------------------------------------------------


def default_telegram_sender() -> Optional[TelegramSender]:
    """Best-effort sender of the grill message; returns `None` (no-op) if unconfigured.

    Grill questions and their inline buttons belong to the ops bot (@darkfac_ops_bot), so the
    ops token is used whenever `TELEGRAM_OPS_BOT_TOKEN` is set; otherwise the owner bot is the
    fallback. Alerts (canary failures, human requests) stay on the owner bot elsewhere.
    """
    try:
        from core.integrations.telegram import TelegramGateway, load_telegram_config
    except Exception:  # pragma: no cover - defensive import guard
        return None
    role = "ops" if os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "").strip() else "owner"
    try:
        config = load_telegram_config(role=role)
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


def resolve_callback_choice(question: _PendingQuestion, choice: str) -> str:
    """Map a callback `<choice>` (option index) back to the option text."""
    if choice.isdigit() and int(choice) < len(question.options):
        return question.options[int(choice)]
    return choice


def _build_message(run_id: str, questions: list[_PendingQuestion]) -> tuple[str, list[list[dict[str, str]]]]:
    # The gateway sends with parse_mode=HTML, so agent text must be escaped.
    # Buttons carry the option *index*: Telegram caps callback_data at 64 bytes.
    hub_url = os.environ.get("DARKHUB_PUBLIC_URL") or os.environ.get("DARKHUB_BASE_URL", "http://127.0.0.1:8888")
    lines = [
        f"Grill pendente para o run {html.escape(run_id)} "
        f"(uma rodada, prazo de {GRILL_DEADLINE_HOURS}h):",
        "",
    ]
    buttons: list[list[dict[str, str]]] = []
    for question in questions:
        lines.append(f"- [{question.kind}] {html.escape(question.text)}")
        if question.recommended:
            lines.append(f"  recomendado: {html.escape(question.recommended)}")
        row = [
            {"text": option[:64], "callback_data": f"cb:grill:{run_id}#{question.id}:{index}"}
            for index, option in enumerate(question.options)
        ]
        if row:
            buttons.append(row)

    lines.append("")
    lines.append(f'🎙️ <b>Voz/Áudio:</b> Grave uma nota de voz nesta conversa ou <a href="{hub_url}">abra no DarkHub</a>.')
    buttons.append([{"text": "🎙️ Abrir DarkHub (Voz/Grill)", "url": f"{hub_url}"}])
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
    parent_grill: Optional[str] = None,
    auto_policy: bool = False,
    answers: Optional[dict[str, str]] = None,
) -> StageResult:
    """Run (or reconcile) the single-round grill for `run_id`.

    `auto_policy` (canary runs, see `is_auto_grill`): intent/business questions are resolved
    immediately with their recommended answer, tagged `auto_canary`, with no Telegram message.
    `secret`/`account` questions still block.

    `answers` are owner answers recorded in the control store (`{question_id: raw callback
    choice}`, see `core.line.human.build_telegram_line_grill_handler`); a pending grill applies
    them on reconcile, so a process without git access (the DarkHub) can answer a grill.

    Idempotent: a finished grill is never re-run; a pending grill is
    resolved (fully or partially) rather than restarted. `parent_grill` is
    the parent run's `GRILL.md` for milestone child demands (payload key
    `parent_grill`), so the agent only asks what is new.
    """
    current_time = _now(now)
    ws = workspace.checkout(project, run_id)

    existing = workspace.find_commit_by_job(ws, _job_key(run_id))
    if existing:
        return StageResult(outcome="success", output_refs=[existing])

    pending = _load_pending(ws)
    if pending is not None:
        return _reconcile_pending(
            ws, run_id, pending, current_time, send_message, auto_policy=auto_policy, answers=answers
        )

    workspace.write_context(ws, _DEMAND_FILE, _render_demand_markdown(project, channel, demand_text))

    prompt = render_prompt(
        load_prompt("grill.md"),
        demand=demand_text,
        grill=parent_grill or "(nenhuma premissa anterior)",
        lessons=read_lessons(ws.path) or "(nenhuma)",
    )
    result = run_read_agent(STAGE, prompt, ws.path, host_caps=host_caps, routing_config=routing_config)
    if not result.ok:
        return retry_for_agent_failure(result, "grill_agent_failed", now=current_time)

    try:
        plan = parse_grill_plan(result.text)
    except (ValueError, json.JSONDecodeError, ValidationError):
        return StageResult(outcome="failed", cause_code="grill_invalid_json")

    questions = plan.questions[:MAX_QUESTIONS]
    technical = [_PendingQuestion(**q.model_dump()) for q in questions if q.kind == "technical"]
    blocking = [_PendingQuestion(**q.model_dump()) for q in questions if q.kind in _BLOCKING_KINDS]

    auto_resolved: list[dict[str, Any]] = []
    if auto_policy and blocking:
        auto_resolved, blocking = _split_auto_resolvable(blocking)

    if not blocking:
        markdown = _render_grill_markdown(
            assumptions=plan.assumptions, technical=technical, resolved=auto_resolved, pending=[]
        )
        workspace.write_context(ws, GRILL_FILE, markdown)
        message = "grill: auto-resolved (canary)" if auto_resolved else "grill: no blocking questions"
        sha = workspace.commit(ws, message, _job_key(run_id))
        workspace.push(ws)
        return StageResult(outcome="success", output_refs=[sha])

    deadline = current_time + timedelta(hours=GRILL_DEADLINE_HOURS)
    pending_state = PendingGrill(
        assumptions=plan.assumptions,
        technical=technical,
        questions=blocking,
        deadline=deadline,
        auto_resolved=auto_resolved,
    )
    markdown = _render_grill_markdown(
        assumptions=plan.assumptions, technical=technical, resolved=auto_resolved, pending=blocking
    )
    workspace.write_context(ws, GRILL_FILE, markdown)
    _save_pending(ws, pending_state)
    sha = workspace.commit(ws, "grill: awaiting owner decisions", _pending_job_key(run_id))
    workspace.push(ws)

    sha = _notify_owner(ws, run_id, pending_state, send_message) or sha

    return StageResult(
        outcome="waiting_human",
        output_refs=[sha],
        evidence_refs=[f"grill_deadline:{deadline.isoformat()}"],
    )


def _notify_owner(
    ws: workspace.RunWorkspace,
    run_id: str,
    pending: PendingGrill,
    send_message: Optional[TelegramSender],
) -> Optional[str]:
    """Send the single grill message once; record `notified` so a crash or a
    failed send is retried on the next reconcile instead of silently lost."""
    if pending.notified:
        return None
    sender = send_message if send_message is not None else default_telegram_sender()
    if sender is None:
        return None
    text, buttons = _build_message(run_id, pending.questions)
    try:
        sent = bool(sender(text, buttons))
    except Exception as exc:  # notification must never break the stage
        logger.warning("Failed to send grill Telegram message for run %s: %s", run_id, exc)
        sent = False
    if not sent:
        return None
    pending.notified = True
    _save_pending(ws, pending)
    sha = workspace.commit(ws, "grill: owner notified", _pending_job_key(run_id))
    workspace.push(ws)
    return sha


def _reconcile_pending(
    ws: workspace.RunWorkspace,
    run_id: str,
    pending: PendingGrill,
    current_time: datetime,
    send_message: Optional[TelegramSender] = None,
    *,
    auto_policy: bool = False,
    answers: Optional[dict[str, str]] = None,
) -> StageResult:
    by_id = {q.id: q for q in pending.questions}
    for question_id, choice in (answers or {}).items():
        question = by_id.get(question_id)
        if question is not None and question_id not in pending.answers:
            pending.answers[question_id] = resolve_callback_choice(question, choice)

    deadline = pending.deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    deadline_passed = current_time >= deadline

    resolvable: list[dict[str, Any]] = list(pending.auto_resolved)
    still_pending: list[_PendingQuestion] = []
    auto_ids: set[str] = set()
    for question in pending.questions:
        answer = pending.answers.get(question.id)
        auto_answer = _auto_answer(question) if auto_policy else None
        if answer is not None:
            resolvable.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": answer, "note": "respondida pelo owner"}
            )
        elif auto_answer is not None:
            # Canary: never wait for the 12h deadline, resolve now with the recommended answer.
            auto_ids.add(question.id)
            resolvable.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": auto_answer, "note": AUTO_CANARY_NOTE}
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

    ready_to_finalize = deadline_passed or all(
        q.id in pending.answers or q.id in auto_ids for q in pending.questions
    )

    if not ready_to_finalize:
        _notify_owner(ws, run_id, pending, send_message)
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
