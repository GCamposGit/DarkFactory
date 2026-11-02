"""Owner-decision Grill stage for the DarkFac production line (HF-27-04).

Implements section "Grill" of `docs/handoffs/production-line/HF-27-04.md`
and the owner decision in `docs/PRODUCTION_LINE_PLAN_2026-09-22.md` (section
2.4): owner decisions remain pending until explicitly answered. Each
notification displays up to three unanswered questions; the deadline never
selects an answer.

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
     `<run_id>:grill:pending`), push, notify the first three unanswered
     questions, and return `waiting_human` with a `deadline` evidence ref.
4. If a pending grill state already exists, apply explicit owner answers.
   When the current page is answered, notify the next page. Finalize only
   after all material decisions are answered or an explicitly authorized
   canary policy resolves eligible questions. The deadline never supplies
   an answer or clears pending state.

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
from core.line import diagnostics
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.route_wait import RouteWaiter
from core.line.routing import RoutingConfig, harness_cli_pin, pick, record_result
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult

logger = logging.getLogger(__name__)

STAGE = "grill"
GRILL_DEADLINE_HOURS = 12
MAX_QUESTIONS = 3
_NO_DEFAULT_KINDS = frozenset({"secret", "account", "new_secret"})
# Owner questions live in a closed coded set (USR-375 S6); legacy kinds map onto it and
# anything else is technical: decided by the factory and recorded as an ADR in GRILL.md.
_KIND_TO_CATEGORY = {"intent": "objective", "secret": "new_secret", "account": "new_secret"}
_OWNER_CATEGORIES = frozenset({"objective", "success", "business", "new_secret", "acceptance"})
_BLOCKING_KINDS = frozenset({"business", "intent", "secret", "account", "objective", "success", "new_secret", "acceptance"})
# Canary auto-resolution is enabled only by the explicit run payload policy.
AUTO_GRILL_POLICY = "auto"
AUTO_CANARY_NOTE = "auto_canary"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_DEMAND_FILE = "DEMAND.md"
GRILL_FILE = "GRILL.md"
_GRILL_FILE = GRILL_FILE  # internal alias
_PENDING_FILE = "GRILL_PENDING.json"

ADOPTED_EVIDENCE_PREFIX = "grill_adopted:"
# Heading `_render_grill_markdown` adds only while questions are still unresolved.
_PENDING_SECTION = "## Aguardando decisao do owner"

TelegramSender = Callable[[str, list[list[dict[str, str]]]], bool]


# --------------------------------------------------------------------------
# Agent-facing contracts
# --------------------------------------------------------------------------


class GrillQuestion(BaseModel):
    """One question the grill agent wants the owner (or itself) to resolve."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: str = "technical"
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
    # Empty on legacy state means `notified` refers to the original first page.
    notified_question_ids: list[str] = Field(default_factory=list)
    round_pages: list[list[str]] = Field(default_factory=list)
    round_index: int = 0
    round_visit: int = 0
    notified_round_visit: int = -1
    round_answer_snapshot: list[str] = Field(default_factory=list)
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


def extract_json_object(text: str, expected_keys: Iterable[str] = ()) -> str:
    """The first top-level JSON object embedded in an agent reply.

    Tolerates code fences, leading/trailing prose, several objects and stray braces in the prose:
    every `{` is tried with `JSONDecoder.raw_decode`, nested objects of an already decoded one are
    skipped, and the first decoded object carrying one of `expected_keys` wins (the first decodable
    object when none does). Raises `ValueError` when nothing decodes.
    """
    source = (text or "").strip()
    decoder = json.JSONDecoder()
    wanted = set(expected_keys)
    first: Optional[str] = None
    index = source.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(source, index)
        except ValueError:
            index = source.find("{", index + 1)
            continue
        if isinstance(value, dict):
            candidate = source[index:end]
            if not wanted or wanted & set(value):
                return candidate
            if first is None:
                first = candidate
        index = source.find("{", end)
    if first is not None:
        return first
    raise ValueError("no JSON object found in agent output")


def _extract_json_object(text: str) -> str:
    return extract_json_object(text)


_GRILL_KEYS = ("questions", "assumptions", "is_product_scale")


def parse_grill_plan(text: str) -> GrillPlan:
    """Parse and validate an agent's grill JSON reply. Raises on malformed input."""
    candidate = extract_json_object(text, _GRILL_KEYS)
    data = json.loads(candidate)
    return GrillPlan.model_validate(data)


# Agent failures that only a human/ops action can fix (log a harness in, set a
# token). Retrying immediately would just burn iterations, so the retry carries
# a `not_before=<iso>` token (successors.py D-c) that also bounds the wait by
# the run's wall clock instead of the same-stage count cap.
_WAIT_FOR_AUTH_KINDS = frozenset({"no_authenticated_harness", "auth_expired"})
AUTH_RETRY_BACKOFF_MINUTES = 10


def retry_for_agent_failure(
    result: AgentResult,
    fallback_cause: str,
    *,
    now: Optional[datetime] = None,
    on_no_route: Optional[Callable[[], StageResult]] = None,
) -> StageResult:
    """`retry` StageResult for a failed agent call, with a short cause code.

    `<error_kind>` normally; for auth-type failures
    `<error_kind> not_before=<iso>` (<= 64 chars, the persisted column width).

    `on_no_route` (USR-87) answers the "`pick()` found nothing routable" result of `run_read_agent`
    (`no_authenticated_harness`, `harness="none"`): the stage hands over its `RouteWaiter`, which
    waits for the earliest cooldown/quota reset (`no_route_available not_before=<iso>`) and only
    parks the run past its wall-clock budget. Without it the legacy 10-minute auth wait applies.
    """
    kind = result.error_kind or fallback_cause
    if on_no_route is not None and result.error_kind == "no_authenticated_harness":
        return on_no_route()
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
    exclude: Iterable[tuple[str, Optional[str]]] = (),
) -> AgentResult:
    """Run one read-mode agent call, trying the stage's cascade until one answers.

    Shared by grill and planning: walks `routing.pick()` excluding harnesses
    that already failed in this call, recording cooldowns for rate-limited
    accounts. Returns the last (failing) `AgentResult` if nothing worked.
    """
    excluded: set[tuple[str, Optional[str]]] = set(exclude)
    last_result: Optional[AgentResult] = None
    for _ in range(max_attempts):
        choice = pick(
            stage,
            host_caps,
            exclude=excluded,
            config=routing_config,
        )
        if choice is None and last_result is None and excluded:
            # Every route was excluded by earlier failed attempts: better to retry one than wait forever.
            excluded = set()
            choice = pick(stage, host_caps, exclude=excluded, config=routing_config)
        if choice is None:
            break
        harness, model = choice
        cli_model, cli_effort = harness_cli_pin(harness, model, stage=stage, config=routing_config)
        req = AgentRequest(prompt=prompt, cwd=cwd, mode="read", harness=harness, model=cli_model, effort=cli_effort)
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


# Attempts (per stage, recorded on the run branch) after which bad JSON is a terminal failure.
MAX_INVALID_JSON_ATTEMPTS = 3
INVALID_JSON_RETRY_MINUTES = 1
# Failures that are not the harness's fault (login, quota, missing binary) and retry on their own
# schedule: recording each of them would only spam the run branch.
_QUIET_FAILURE_KINDS = frozenset({"rate_limited", "auth_expired", "not_installed", "no_authenticated_harness"})


def _record_failed_agent(
    ws: workspace.RunWorkspace, run_id: str, stage: str, result: AgentResult, note: str = ""
) -> list[dict[str, Any]]:
    """Record + publish one failed agent attempt (skipped for login/quota/missing-binary kinds)."""
    if result.error_kind in _QUIET_FAILURE_KINDS:
        return diagnostics.load_attempts(ws, stage)
    attempts = diagnostics.record_attempt(
        ws, stage, harness=result.harness, model=result.model, error_kind=result.error_kind or note or "failed",
        duration_s=result.duration_s, output=result.text, note=note,
    )
    diagnostics.persist(ws, f"chore(line): {stage} agent attempt {len(attempts)} failed", f"{run_id}:{stage}:diag")
    return attempts


def invalid_json_outcome(
    ws: workspace.RunWorkspace,
    run_id: str,
    stage: str,
    result: AgentResult,
    cause_code: str,
    *,
    now: Optional[datetime] = None,
) -> StageResult:
    """Outcome for an agent reply that is not valid JSON: retry (other harness, short delay), then fail.

    The first bad outputs are `retry` (the store's retry count also caps it), with the offending
    harness excluded next time through the attempts file; only after
    `MAX_INVALID_JSON_ATTEMPTS` recorded attempts is it terminal.
    """
    attempts = _record_failed_agent(ws, run_id, stage, result, note="invalid_json")
    invalid = sum(1 for a in attempts if a.get("note") == "invalid_json")
    if invalid >= MAX_INVALID_JSON_ATTEMPTS:
        return StageResult(outcome="failed", cause_code=cause_code)
    when = (now or datetime.now(timezone.utc)) + timedelta(minutes=INVALID_JSON_RETRY_MINUTES)
    return StageResult(outcome="retry", cause_code=f"{cause_code} not_before={when.replace(microsecond=0).isoformat()}")


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
                f"(auto_technical; {question.why or 'decisao tecnica reversivel'})"
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
    """True only when this run explicitly carries the authorized canary policy."""
    del project  # Kept in the public signature for callers and compatibility.
    return (payload or {}).get("grill_policy") == AUTO_GRILL_POLICY


def _auto_answer(question: _PendingQuestion) -> Optional[str]:
    """The answer a canary auto-resolves `question` with, or None if it must keep blocking."""
    if question.kind in _NO_DEFAULT_KINDS:
        return None
    if question.recommended:
        return question.recommended
    return question.options[0] if question.options else None


def _legacy_timeout_questions(markdown: str) -> list[_PendingQuestion]:
    """Recover owner decisions that older runs finalized from silence."""
    item_pattern = re.compile(
        r"^\s*-\s+\[(?P<kind>[^\]]+)\]\s+(?P<body>[\s\S]*?)"
        r"(?=^\s*-\s+\[[^\]]+\]|\Z)",
        re.MULTILINE,
    )
    questions: list[_PendingQuestion] = []
    items = list(item_pattern.finditer(markdown))
    for item in items:
        if "default_after_timeout" not in item.group("body"):
            continue
        decision = re.search(
            r"^(?P<text>[\s\S]*?)\s*->\s+\*\*(?P<chosen>[\s\S]*?)\*\*"
            r"\s+\(default_after_timeout\)\s*$",
            item.group("body").strip(),
        )
        if decision is None:
            continue
        chosen = decision.group("chosen").strip()
        questions.append(
            _PendingQuestion(
                id=f"legacy_timeout_{len(questions) + 1}",
                text=decision.group("text").strip(),
                kind=item.group("kind").strip(),
                recommended=chosen or None,
                why="Escolha registrada por prazo em versao antiga; requer confirmacao explicita.",
            )
        )
    if markdown.count("default_after_timeout") > len(questions):
        questions.append(
            _PendingQuestion(
                id="legacy_timeout_unparsed",
                text=(
                    "Confirme explicitamente uma decisao material registrada por prazo em uma versao antiga. "
                    "Informe a escolha manualmente para a fabrica prosseguir."
                ),
                kind="business",
            )
        )
    return questions


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
        pending = PendingGrill.model_validate(data)
        # Older versions moved owner decisions after the first three into
        # `technical`. Restore those decisions to the material queue.
        legacy_material = []
        technical = []
        question_ids = {question.id for question in pending.questions}
        for question in pending.technical:
            category = _KIND_TO_CATEGORY.get(question.kind, question.kind)
            if category in _OWNER_CATEGORIES and question.id not in question_ids:
                legacy_material.append(question)
                question_ids.add(question.id)
            else:
                technical.append(question)
        if legacy_material:
            pending.questions.extend(legacy_material)
            pending.technical = technical
        if not pending.round_pages:
            pending.round_pages = [
                [question.id for question in pending.questions[index:index + MAX_QUESTIONS]]
                for index in range(0, len(pending.questions), MAX_QUESTIONS)
            ]
        return pending
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


DEFAULT_PUBLIC_HUB_URL = "https://darkhub.ggcampos.com"


def safe_public_hub_url(url: Optional[str] = None) -> str:
    """Return a functional public URL for Telegram messages, never 127.0.0.1 or localhost."""
    candidate = (
        url
        or os.environ.get("DARKHUB_PUBLIC_URL")
        or os.environ.get("DARKHUB_BASE_URL")
        or DEFAULT_PUBLIC_HUB_URL
    ).strip().rstrip("/")
    if (
        not candidate
        or "127.0.0.1" in candidate
        or "localhost" in candidate
        or not candidate.startswith("http")
    ):
        return DEFAULT_PUBLIC_HUB_URL
    return candidate


def _clean_option_text(text: str) -> str:
    """Strip redundant prefixes like 'Opção 1:', '1.' or bullets from option text."""
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:opcao|opção)\s*[0-9]*[\s:\.\-]+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^[0-9]+[\.\)\-:\s]+", "", cleaned).strip()
    cleaned = re.sub(r"^[\*\-_•]\s*", "", cleaned).strip()
    return cleaned or text.strip()


def _summarize_option(text: str, max_words: int = 3, max_chars: int = 24) -> str:
    """Extract a concise 2-3 word summary suitable for Telegram buttons on mobile.

    Prevents button text truncation while preserving key intent.
    """
    cleaned = _clean_option_text(text)
    words = [w for w in cleaned.split() if w]
    if not words:
        return "Escolher"
    for count in range(min(len(words), max_words), 0, -1):
        candidate = " ".join(words[:count])
        candidate = re.sub(r"[,;:\.\-]+$", "", candidate).strip()
        if len(candidate) <= max_chars:
            return candidate
    return words[0][:max_chars]


def resolve_callback_choice(question: _PendingQuestion, choice: str) -> str:
    """Map a callback `<choice>` (option index) back to the option text."""
    if choice.isdigit() and int(choice) < len(question.options):
        return question.options[int(choice)]
    return choice


def one_tap_recommended_button(run_id: str, question: _PendingQuestion) -> Optional[dict[str, str]]:
    """One-tap button that answers `question` with its `recommended` option (USR-375 S7).

    The payload is the regular `cb:grill:<run_id>#<question_id>:<option index>` already routed by
    the gateway to `build_telegram_line_grill_handler`, so recording reuses the same path as a
    normal option button. `None` when there is no recommendation or it is not one of the options.
    """
    if not question.recommended or question.recommended not in question.options:
        return None
    index = question.options.index(question.recommended)
    rec_summary = _summarize_option(question.recommended, max_words=3, max_chars=20)
    return {
        "text": f"⭐ Opção {index + 1}: {rec_summary}"[:64],
        "callback_data": f"cb:grill:{run_id}#{question.id}:{index}",
    }


def _build_message(run_id: str, questions: list[_PendingQuestion]) -> tuple[str, list[list[dict[str, str]]]]:
    # The gateway sends with parse_mode=HTML, so agent text must be escaped.
    # Buttons carry the option *index*: Telegram caps callback_data at 64 bytes.
    hub_url = safe_public_hub_url()
    lines = [
        f"🤔 <b>Alinhamento necessário para a tarefa: {html.escape(run_id)}</b>",
        "A fábrica precisa do seu direcionamento para avançar:",
        "",
    ]
    buttons: list[list[dict[str, str]]] = []
    for q_idx, question in enumerate(questions, start=1):
        q_title = f"Pergunta {q_idx}: " if len(questions) > 1 else ""
        lines.append(f"❓ <b>{q_title}{html.escape(question.text)}</b>")
        if question.why:
            lines.append(f"<i>Contexto:</i> {html.escape(question.why)}")

        lines.append("\n<b>Opções disponíveis:</b>")
        for index, option in enumerate(question.options):
            is_rec = bool(question.recommended and option.strip() == question.recommended.strip())
            star = "⭐ " if is_rec else ""
            rec_tag = " <i>(Recomendada)</i>" if is_rec else ""
            lines.append(f"  {star}<b>Opção {index + 1}:</b> {html.escape(_clean_option_text(option))}{rec_tag}")

            # Button for this option: number + 2-3 word summary
            opt_summary = _summarize_option(option, max_words=3, max_chars=24)
            btn_text = f"{index + 1}. {opt_summary}"
            buttons.append([
                {"text": btn_text, "callback_data": f"cb:grill:{run_id}#{question.id}:{index}"}
            ])

        one_tap = one_tap_recommended_button(run_id, question)
        if one_tap is not None:
            buttons.append([one_tap])

    lines.append("")
    lines.append(f'🎙️ <b>Voz ou Detalhes:</b> Responda por áudio aqui no chat ou no <a href="{hub_url}">DarkHub</a>.')
    buttons.append([{"text": "🎙️ Abrir DarkHub", "url": f"{hub_url}"}])
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
    adopt_from_runs: Iterable[str] = (),
    route_waiter: Optional[RouteWaiter] = None,
) -> StageResult:
    """Run (or reconcile) the single-round grill for `run_id`.

    `route_waiter` decides what "no agent route right now" means (USR-87): `retry` until the
    earliest cooldown/quota reset while the run is inside its wall-clock budget, `waiting_human`
    (infra) only after it. Defaults to a waiter that cannot see the run's start (it keeps waiting).

    `adopt_from_runs` (newest first) are earlier attempts of the same ticket (see
    `core.line.owner_intake.previous_attempt_run_ids`): when one of them has a fully resolved grill
    on its branch, it is adopted as is (no agent call, no owner message) and the result carries a
    `grill_adopted:<run_id>` evidence ref. Anything missing or unreadable falls back to the normal
    grill (fail-safe: ask).

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

    pending = _load_pending(ws)
    legacy_timeout_questions: list[_PendingQuestion] = []
    existing = workspace.find_commit_by_job(ws, _job_key(run_id))
    if existing and pending is None:
        missing = grill_artifacts_missing(ws)
        grill_text = ""
        if not missing:
            grill_text = (workspace.context_dir(ws) / GRILL_FILE).read_text(encoding="utf-8")
        if not missing and "default_after_timeout" not in grill_text:
            return StageResult(outcome="success", output_refs=[existing])
        if missing:
            logger.warning(
                "grill job commit %s of run %s has no %s on branch %s; running the grill again",
                existing, run_id, ", ".join(missing), ws.branch,
            )
        else:
            logger.warning(
                "grill job commit %s of run %s contains a legacy timeout choice; rerunning the grill",
                existing, run_id,
            )
            legacy_timeout_questions = _legacy_timeout_questions(grill_text)

    if pending is not None:
        return _reconcile_pending(
            ws, run_id, pending, current_time, send_message, auto_policy=auto_policy, answers=answers
        )

    workspace.write_context(ws, _DEMAND_FILE, _render_demand_markdown(project, channel, demand_text))

    for previous_run_id in adopt_from_runs:
        adopted = _adopt_previous_grill(ws, run_id, previous_run_id)
        if adopted:
            return StageResult(
                outcome="success", output_refs=[adopted], evidence_refs=[f"{ADOPTED_EVIDENCE_PREFIX}{previous_run_id}"]
            )

    prompt = render_prompt(
        load_prompt("grill.md"),
        demand=demand_text,
        grill=parent_grill or "(nenhuma premissa anterior)",
        lessons=read_lessons(ws.path) or "(nenhuma)",
    )
    previous = diagnostics.load_attempts(ws, STAGE)
    result = run_read_agent(
        STAGE, prompt, ws.path, host_caps=host_caps, routing_config=routing_config,
        exclude=diagnostics.failed_pairs(previous),
    )
    if not result.ok:
        _record_failed_agent(ws, run_id, STAGE, result)
        waiter = route_waiter or RouteWaiter()
        return retry_for_agent_failure(
            result, "grill_agent_failed", now=current_time,
            on_no_route=lambda: waiter.no_route_result(
                STAGE, project, run_id, host_caps=host_caps, config=routing_config
            ),
        )

    try:
        plan = parse_grill_plan(result.text)
    except (ValueError, json.JSONDecodeError, ValidationError):
        return invalid_json_outcome(ws, run_id, STAGE, result, "grill_invalid_json", now=current_time)

    technical: list[_PendingQuestion] = []
    blocking: list[_PendingQuestion] = []
    for q in plan.questions:
        pq = _PendingQuestion(**q.model_dump())
        category = _KIND_TO_CATEGORY.get(q.kind, q.kind)
        if category in _OWNER_CATEGORIES:
            if not pq.recommended and pq.options:
                pq.recommended = pq.options[0]
            blocking.append(pq)
        else:
            technical.append(pq)

    blocking_texts = {question.text.strip().casefold() for question in blocking}
    blocking.extend(
        question for question in legacy_timeout_questions
        if question.text.strip().casefold() not in blocking_texts
    )

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
        round_pages=[
            [question.id for question in blocking[index:index + MAX_QUESTIONS]]
            for index in range(0, len(blocking), MAX_QUESTIONS)
        ],
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


def grill_artifacts_missing(ws: workspace.RunWorkspace) -> list[str]:
    """Names of the grill context files (`DEMAND.md`, `GRILL.md`) absent or empty in the run's checkout."""
    missing: list[str] = []
    for name in (_DEMAND_FILE, GRILL_FILE):
        path = workspace.context_dir(ws) / name
        try:
            present = path.is_file() and bool(path.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            present = False
        if not present:
            missing.append(name)
    return missing


def _adopt_previous_grill(ws: workspace.RunWorkspace, run_id: str, previous_run_id: str) -> Optional[str]:
    """Commit `previous_run_id`'s resolved GRILL.md as this run's grill; the commit SHA, or None.

    Only a grill that finished on the previous run's branch (its `<run>:grill` job commit exists) and
    has nothing left pending is adopted. Every failure (branch gone, file unreadable, git error) returns
    None so the caller runs the normal grill.
    """
    if not previous_run_id or previous_run_id == run_id:
        return None
    ref = f"origin/df/{previous_run_id}"
    try:
        if workspace.find_commit_by_job(ws, _job_key(previous_run_id), ref=ref) is None:
            return None
        text = workspace.read_file_at(ws, ref, f".darkfac/runs/{previous_run_id}/{GRILL_FILE}")
        if not text or not text.strip() or _PENDING_SECTION in text or "default_after_timeout" in text:
            return None
        body = text.lstrip("﻿").strip("\n")
        first, _, rest = body.partition("\n")
        if first.strip() != "# GRILL":
            return None
        note = (
            f"> Decisoes adotadas da tentativa anterior do mesmo ticket (run {previous_run_id}); "
            "o owner nao foi consultado de novo."
        )
        workspace.write_context(ws, GRILL_FILE, f"# GRILL\n\n{note}\n{rest}\n")
        sha = workspace.commit(ws, f"grill: adopted from previous attempt {previous_run_id}", _job_key(run_id))
    except Exception as exc:  # any doubt -> ask the owner through the normal grill
        logger.warning("Could not adopt grill of %s for run %s: %s", previous_run_id, run_id, exc)
        return None
    workspace.push(ws)  # like the normal grill, a push failure is the stage's to retry
    return sha


def _alert_owner_bot(text: str, alert_sender: Optional[Callable[[str], bool]] = None) -> bool:
    """Best-effort alert to the owner bot when the ops bot notification fails."""
    try:
        if alert_sender is not None:
            return bool(alert_sender(text))
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        from core.line.human import _default_sender

        sender = _default_sender()
        if sender is not None:
            return bool(sender(text))
    except Exception as exc:  # pragma: no cover - alert must never break the stage
        logger.warning("Failed to send alert to owner bot: %s", exc)
    return False


def _notify_owner(
    ws: workspace.RunWorkspace,
    run_id: str,
    pending: PendingGrill,
    send_message: Optional[TelegramSender],
) -> Optional[str]:
    """Notify the current page once; failed sends remain eligible for retry."""
    page_ids = pending.round_pages[pending.round_index] if pending.round_pages else [
        q.id for q in pending.questions[:MAX_QUESTIONS]
    ]
    by_id = {question.id: question for question in pending.questions}
    current_page = [by_id[question_id] for question_id in page_ids if question_id in by_id and question_id not in pending.answers]
    if not current_page:
        return None
    current_ids = [q.id for q in current_page]
    previously_notified = pending.notified_question_ids
    if pending.notified and not previously_notified:
        # Legacy pending files had one notification containing the first page.
        previously_notified = [q.id for q in pending.questions[:MAX_QUESTIONS]]
    same_legacy_page = pending.notified_round_visit < 0 and set(current_ids).issubset(previously_notified)
    same_round = pending.notified_round_visit == pending.round_visit
    if pending.notified and (same_legacy_page or same_round):
        return None
    if pending.notified_round_visit != pending.round_visit:
        pending.notified = False
    sender = send_message if send_message is not None else default_telegram_sender()
    if sender is None:
        return None
    text, buttons = _build_message(run_id, current_page)
    try:
        sent = bool(sender(text, buttons))
    except Exception as exc:  # notification must never break the stage
        logger.error("Failed to send grill Telegram message for run %s: %s", run_id, exc)
        sent = False
    if not sent:
        logger.error("Grill Telegram notification not delivered for run %s (notified=false)", run_id)
        alert_msg = (
            f"[ALERTA GRILL] Falha ao enviar perguntas do Grill para o run {run_id} via bot de operacoes. "
            "Verifique credenciais/status do Telegram. O Grill tentara reenviar automaticamente."
        )
        _alert_owner_bot(alert_msg)
        return None
    pending.notified = True
    pending.notified_question_ids = current_ids
    pending.notified_round_visit = pending.round_visit
    pending.round_answer_snapshot = list(pending.answers)
    _save_pending(ws, pending)
    sha = workspace.commit(ws, "grill: owner notified", _pending_job_key(run_id))
    workspace.push(ws)
    return sha


def resend_unnotified_grill(
    project: ProjectDescriptor,
    run_id: str,
    send_message: Optional[TelegramSender] = None,
) -> bool:
    """If GRILL_PENDING.json exists on the run branch with notified=False, attempt to notify the owner.

    Returns True if notified (already was or just sent successfully), False if delivery failed.
    """
    try:
        ws = workspace.checkout(project, run_id)
        pending = _load_pending(ws)
        if pending is None:
            return True
        if pending.notified and pending.notified_round_visit == pending.round_visit:
            return True
        sha = _notify_owner(ws, run_id, pending, send_message)
        return sha is not None or (pending.notified and pending.notified_round_visit < 0)
    except Exception as exc:
        logger.warning("resend_unnotified_grill failed for run %s: %s", run_id, exc)
        return False


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

    if pending.round_pages:
        current_round = pending.round_pages[pending.round_index]
        newly_answered = set(pending.answers) - set(pending.round_answer_snapshot)
        if newly_answered.intersection(current_round):
            for next_index in range(pending.round_index + 1, len(pending.round_pages)):
                next_page = pending.round_pages[next_index]
                if any(
                    question_id not in pending.answers
                    and not (auto_policy and _auto_answer(by_id[question_id]) is not None)
                    for question_id in next_page
                    if question_id in by_id
                ):
                    pending.round_index = next_index
                    pending.round_visit += 1
                    pending.notified = False
                    break

    resolvable: list[dict[str, Any]] = list(pending.auto_resolved)
    still_pending: list[_PendingQuestion] = []
    for question in pending.questions:
        answer = pending.answers.get(question.id)
        auto_answer = _auto_answer(question) if auto_policy else None
        if answer is not None:
            resolvable.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": answer, "note": "respondida pelo owner"}
            )
        elif auto_answer is not None:
            # Canary: never wait for the 12h deadline, resolve now with the recommended answer.
            resolvable.append(
                {"id": question.id, "text": question.text, "kind": question.kind, "chosen": auto_answer, "note": AUTO_CANARY_NOTE}
            )
        else:
            still_pending.append(question)

    ready_to_finalize = not still_pending

    if not ready_to_finalize:
        workspace.write_context(
            ws,
            GRILL_FILE,
            _render_grill_markdown(
                assumptions=pending.assumptions,
                technical=pending.technical,
                resolved=resolvable,
                pending=still_pending,
            ),
        )
        _save_pending(ws, pending)
        workspace.commit(ws, "grill: awaiting owner decisions", _pending_job_key(run_id))
        workspace.push(ws)
        _notify_owner(ws, run_id, pending, send_message)
        _save_pending(ws, pending)
        return StageResult(
            outcome="waiting_human",
            evidence_refs=[f"grill_deadline:{pending.deadline.isoformat()}"],
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

    return StageResult(outcome="success", output_refs=[sha])


def _render_demand_markdown(project: ProjectDescriptor, channel: str, demand_text: str) -> str:
    return (
        f"# DEMAND\n\n"
        f"- projeto: {project.id}\n"
        f"- canal: {channel}\n\n"
        f"## Texto original\n\n{demand_text}\n"
    )
