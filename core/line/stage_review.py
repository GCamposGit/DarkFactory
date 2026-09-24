"""Cross-family review stage for the DarkFac production line (HF-27-05).

Implements the "Review" section of `docs/handoffs/production-line/HF-27-05.md`:

1. Computes `git diff origin/<default>...HEAD` for the run's branch,
   summarizing it when it exceeds 60 KB.
2. Runs a read-mode agent from a *different family* than the one recorded
   in `progress.json` (written by `core.line.stage_build.DevelopmentStage`),
   via `core.line.routing.pick(stage="review", ...)`'s
   `other_family_than_development` cascade rule.
3. Parses the agent's JSON verdict (`approve` or `changes_required`) and
   commits `review-<n>.md`. `changes_required` maps to `retry` (back to
   development) up to `run_caps.review_rounds` (default 2); the round after
   that force-approves — annotated with the outstanding `non_blocking`
   items — as long as the last `ValidationStage` run was green, to avoid an
   infinite loop between two models with differing opinions.

All round state lives in `.darkfac/runs/<run_id>/review_state.json` on the
run's branch, so review is resumable and idempotent exactly like
development: replaying an already-resolved round returns its recorded
outcome without invoking the agent again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from core.line import workspace
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import RoutingConfig, load_routing_config, pick, record_result
from core.line.stage_build import fill_template
from core.line.workspace import RunWorkspace
from core.projects.models import ProjectDescriptor
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

_MAX_DIFF_BYTES = 60 * 1024
# The run's own context files (logs, reviews, progress) are not product code.
_DIFF_PATHSPEC = (".", ":(exclude).darkfac/runs")

_FALLBACK_REVIEW_TEMPLATE = """\
Voce e um revisor independente de codigo da DarkFac, de familia diferente de quem
implementou esta mudanca. Responda apenas com um objeto JSON no formato:
{{"verdict": "approve" ou "changes_required",
  "blocking": [{{"file": "...", "line": 0, "issue": "...", "fix": "..."}}],
  "non_blocking": [{{"file": "...", "line": 0, "issue": "...", "fix": "..."}}]}}

So marque como bloqueante: bug de correcao, teste que nao testa o aceite,
requisito do ticket nao atendido, ou quebra de contrato.

A SPEC e os tickets (com criterios de aceite) estao em `{context_dir}/SPEC.md`
e `{context_dir}/tickets.json` neste repositorio; leia-os antes de julgar.

## Diff (rodada {round_num})
{diff_text}
"""


class ReviewFinding(BaseModel):
    file: str = ""
    line: Optional[int] = None
    issue: str = ""
    fix: str = ""


class ReviewVerdict(BaseModel):
    verdict: str
    blocking: list[ReviewFinding] = Field(default_factory=list)
    non_blocking: list[ReviewFinding] = Field(default_factory=list)


class ReviewRound(BaseModel):
    round: int
    verdict: str
    sha: Optional[str] = None
    forced: bool = False


class ReviewState(BaseModel):
    rounds: list[ReviewRound] = Field(default_factory=list)


def _load_prompt_template(name: str, fallback: str) -> str:
    """Read `core/line/prompts/<name>` if HF-27-04 has landed it; else use `fallback`."""
    candidate = _PROMPTS_DIR / name
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read prompt template %s (%s); using fallback", candidate, exc)
    return fallback


def _read_progress_harness(ws: RunWorkspace) -> Optional[str]:
    path = workspace.context_dir(ws) / "progress.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        harness = data.get("harness")
        return str(harness) if harness else None
    return None


def _read_validation_green(ws: RunWorkspace) -> bool:
    path = workspace.context_dir(ws) / "validation.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    commands = data.get("commands")
    if not isinstance(commands, dict):
        return False
    for entry in commands.values():
        if isinstance(entry, dict) and entry.get("ran") and not entry.get("ok", True):
            return False
    return True


def _load_review_state(ws: RunWorkspace) -> ReviewState:
    path = workspace.context_dir(ws) / "review_state.json"
    if not path.is_file():
        return ReviewState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ReviewState()
    try:
        return ReviewState.model_validate(raw)
    except ValidationError:
        return ReviewState()


def _write_review_state(ws: RunWorkspace, state: ReviewState) -> None:
    workspace.write_context(ws, "review_state.json", state.model_dump_json(indent=2))


def _get_diff(ws: RunWorkspace, default_branch: str) -> str:
    """`git diff origin/<default>...HEAD`, summarized when larger than 60 KB."""
    proc = workspace._run_git(
        ["diff", f"origin/{default_branch}...HEAD", "--", *_DIFF_PATHSPEC], cwd=ws.path, check=False
    )
    diff_text = proc.stdout or ""
    if len(diff_text.encode("utf-8")) <= _MAX_DIFF_BYTES:
        return diff_text

    stat_proc = workspace._run_git(
        ["diff", "--numstat", f"origin/{default_branch}...HEAD", "--", *_DIFF_PATHSPEC],
        cwd=ws.path,
        check=False,
    )
    rows: list[tuple[int, str]] = []
    for line in (stat_proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        try:
            churn = int(added or 0) + int(removed or 0)
        except ValueError:
            churn = 0
        rows.append((churn, path))
    rows.sort(reverse=True)
    top_files = "\n".join(f"- {path} (+/-{churn} linhas)" for churn, path in rows[:15])
    return (
        f"[diff completo tem {len(diff_text.encode('utf-8'))} bytes, acima do limite de "
        f"{_MAX_DIFF_BYTES} bytes; mostrando apenas o resumo]\n\n"
        f"Arquivos de maior risco (por linhas alteradas):\n{top_files}"
    )


def _render_review_md(round_num: int, verdict: ReviewVerdict, *, forced: bool) -> str:
    lines = [f"# Review round {round_num}", "", f"Verdict: {verdict.verdict}"]
    if forced:
        lines.append("")
        lines.append(
            "(aprovado automaticamente apos esgotar as rodadas de revisao, com a "
            "validacao limpa verde; itens abaixo registrados como nao-bloqueantes)"
        )
    if verdict.blocking:
        lines.append("")
        lines.append("## Bloqueantes")
        for item in verdict.blocking:
            lines.append(f"- `{item.file}:{item.line}` — {item.issue} (fix: {item.fix})")
    if verdict.non_blocking:
        lines.append("")
        lines.append("## Nao-bloqueantes")
        for item in verdict.non_blocking:
            lines.append(f"- `{item.file}:{item.line}` — {item.issue} (fix: {item.fix})")
    lines.append("")
    return "\n".join(lines)


def _parse_verdict(text: str) -> Optional[ReviewVerdict]:
    """Best-effort JSON extraction: the agent may wrap the object in prose or fences."""
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            raw = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        try:
            return ReviewVerdict.model_validate(raw)
        except ValidationError:
            continue
    return None


class ReviewStage:
    """Runs an independent-family, read-mode review of the run's diff."""

    def __init__(
        self,
        *,
        run_agent_func: Callable[[AgentRequest], AgentResult] = run_agent,
        pick_func: Callable[..., Optional[tuple[str, Optional[str]]]] = pick,
        host_caps: list[str] | tuple[str, ...] = _DEFAULT_HOST_CAPS,
        routing_config: Optional[RoutingConfig] = None,
        agent_timeout_s: int = 1800,
    ) -> None:
        self.run_agent_func = run_agent_func
        self.pick_func = pick_func
        self.host_caps = list(host_caps)
        self.routing_config = routing_config or load_routing_config()
        self.agent_timeout_s = agent_timeout_s

    def run(self, project: ProjectDescriptor, run_id: str) -> StageResult:
        ws = workspace.checkout(project, run_id)
        state = _load_review_state(ws)

        if state.rounds and state.rounds[-1].verdict == "approve":
            last = state.rounds[-1]
            last_sha = last.sha or workspace.find_commit_by_job(ws, f"{run_id}:review:{last.round}")
            return StageResult(outcome="success", output_refs=[last_sha] if last_sha else [])

        round_num = len(state.rounds) + 1
        max_rounds = self.routing_config.run_caps.review_rounds
        implementing_harness = _read_progress_harness(ws)

        route = self.pick_func(
            "review",
            self.host_caps,
            implementing_harness=implementing_harness,
            config=self.routing_config,
        )
        if route is None:
            return StageResult(outcome="waiting_human", cause_code="no_route_available", output_refs=[])
        harness, model = route

        diff_text = _get_diff(ws, project.default_branch or "main")
        template = _load_prompt_template("review.md", _FALLBACK_REVIEW_TEMPLATE)
        prompt = fill_template(
            template,
            round_num=str(round_num),
            diff_text=diff_text,
            context_dir=f".darkfac/runs/{run_id}",
        )

        agent_result = self.run_agent_func(
            AgentRequest(
                prompt=prompt,
                cwd=ws.path,
                mode="read",
                harness=harness,
                model=model,
                timeout_s=self.agent_timeout_s,
            )
        )
        record_result(agent_result, config=self.routing_config)

        if not agent_result.ok:
            return StageResult(
                outcome="retry", cause_code=f"review_agent_{agent_result.error_kind}", output_refs=[]
            )

        verdict = _parse_verdict(agent_result.text)
        if verdict is None:
            return StageResult(outcome="retry", cause_code="review_invalid_json", output_refs=[])

        exhausted = round_num > max_rounds
        force_approve = exhausted and verdict.verdict != "approve"
        if force_approve and not _read_validation_green(ws):
            return StageResult(outcome="failed", cause_code="review_exhausted_not_green", output_refs=[])

        approved = verdict.verdict == "approve" or force_approve
        content = _render_review_md(round_num, verdict, forced=force_approve)
        workspace.write_context(ws, f"review-{round_num}.md", content)

        state.rounds.append(
            ReviewRound(round=round_num, verdict="approve" if approved else "changes_required", forced=force_approve)
        )
        _write_review_state(ws, state)

        job_key = f"{run_id}:review:{round_num}"
        sha = workspace.commit(
            ws,
            f"docs: review round {round_num}" + (" (changes requested)" if not approved else ""),
            job_key=job_key,
        )
        workspace.push(ws)

        if approved:
            return StageResult(outcome="success", output_refs=[sha])
        # HF-27-08 D-b (review fix item 1): route back to development, not
        # to another review round. The blocking log itself is not carried in
        # the cause_code -- it is `review-{round_num}.md`, committed on the
        # branch above and already consumed by
        # DevelopmentStage._pending_fixup()/_latest_review_log().
        return StageResult(
            outcome="retry",
            cause_code=f"retry:development\nchanges_required:round={round_num}",
            output_refs=[sha],
        )
