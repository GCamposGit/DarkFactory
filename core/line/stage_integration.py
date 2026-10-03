"""GitHub integration stage for the DarkFac production line (HF-27-06).

Turns a reviewed run branch (`df/<run_id>`) into a merge on the target
repo's `default_branch`, idempotently, using the `gh` CLI (assumed already
authenticated on the host — see section 7 item 5 of
`docs/PRODUCTION_LINE_PLAN_2026-09-22.md`). `core.integrations.github`
(`GitHubClient`) and `core.orchestrator.delivery_executor` remain the
reader/reconciler for the older HF-11 hybrid workflow; this module is the
`gh`-CLI lane for the HF-27 production line and does not touch them.

Flow (`IntegrationStageHandler.handle`), per
`docs/handoffs/production-line/HF-27-06.md`:

1. `workspace.checkout` the run, `git fetch origin <default>` and try
   `git rebase origin/<default>`. On conflict: abort the rebase and run a
   **write**-mode agent once per run to resolve it, then validate with
   `commands.validate`. A second conflict (or a validate failure after the
   one resolution attempt already spent) is terminal (`failed(merge_conflict)`
   goes down the "another conflict already tried" path; a `retry` covers
   "the agent/validate failed but the one attempt has not been recorded
   twice yet" — see `_ATTEMPT_JOB_SUFFIX`). Push with `--force-with-lease`,
   only ever on a `df/*` branch.
2. Remove `.darkfac/runs/<run_id>/` from the branch in a final commit — its
   content is folded into the PR body first.
3. Idempotent PR: reuse an already-open PR for the branch, or create one.
4. Evaluate one CI snapshot through `core.git.ci_checks.ensure_green`; pending
   or absent checks return `retry` with a `not_before` hint. The worker never
   sleeps while waiting for checks.
5. A red check downloads the failing job's log, persists it in the run context,
   and returns `retry:development` -- unless the very same check
   is already red on the base branch (`base_red`, USR-86): then no change the
   agent makes can fix it, so the stage returns
   `retry("base_red not_before=<now + 60 min>")` without touching the agent
   or its call budget, at most `ci_checks.BASE_RED_MAX_RETRIES` times (the
   cap is counted per run, see `base_red_attempts`); after that it parks the
   run in `waiting_human` with a `HumanRequest(kind="infra")` and, only for
   the `darkfac` project, best-effort opens the defect ticket for the red
   base (`core.git.autonomy.run_check_main`). Each wait re-runs the stage from
   the top, so a fix landed on the base is rebased into the PR by itself.
6. Merge (`gh pr merge --squash --delete-branch`); falls back to `--auto`
   when a required review blocks it, and to `waiting_human` (repo-setting
   guide) when even that is blocked.
7. Confirms `origin/<default>` actually reached the merge SHA before
   declaring `success` (FACTORY_RULES rule 11).

All subprocess calls use explicit argument lists (never `shell=True`) except
`commands.validate`, whose entries are project-defined shell command strings
(same convention as `core.harness.remote_worker`). Errors are sanitized to
never leak a GitHub token.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from core.git import ci_checks
from core.git.safe_show import safe_show
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult, redact_secrets, run_agent
from core.line.human import HumanRequest, notify_human_request
from core.line.routing import RoutingConfig, pick, record_result
from core.line.workspace import RunWorkspace, WorkspaceError
from core.projects.models import ProjectDescriptor
from core.projects.registry import resolve_commands
from core.workflow.control_contracts import HandlerDescriptor, StageContext, StageResult
from core.workflow.successors import MAX_STAGE_ITERATIONS

logger = logging.getLogger(__name__)

STAGE = "integration"
VERSION = "v1"

# The delay between pending-check snapshots. The caller re-invokes the stage
# after `not_before`; the worker never holds a claim while waiting for CI.
_CI_CHECK_WINDOW_S = 300
_GH_DEFAULT_TIMEOUT_S = 60
_VALIDATE_DEFAULT_TIMEOUT_S = 1800

_ATTEMPT_JOB_SUFFIX = "integration:conflict_attempt"
_RESOLVED_JOB_SUFFIX = "integration:conflict_resolved"
_STRIP_JOB_SUFFIX = "integration:strip_context"
MAX_CI_ITERATIONS = MAX_STAGE_ITERATIONS

_GITHUB_REPO_PATTERN = re.compile(
    r"github\.com[:/]+([^/]+)/([^/.\s]+?)(?:\.git)?/?$", re.IGNORECASE
)

# `base_red` escalation (USR-86). The factory's own project: the only one whose red base is a defect
# of this repository's ticket queue (a client's red CI is the client's to fix).
_FACTORY_PROJECT_ID = "darkfac"
# The `check-main` probe (exit 0 only when the base CI is green) a resume loop can run for `darkfac`.
_FACTORY_BASE_PROBE = "python -m core.git.autonomy check-main --branch {branch}"

BaseRedAttempts = Callable[[str], int]
HumanNotifier = Callable[[HumanRequest], Any]
DefectTicketFunc = Callable[[ci_checks.GhRunner, str], Optional[dict[str, Any]]]


class IntegrationError(RuntimeError):
    """Raised for unrecoverable gh/git plumbing errors inside this stage."""


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _sanitize(text: str) -> str:
    return ci_checks.sanitize(text)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _read_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def owner_repo(repo_url: Optional[str]) -> str:
    """Best-effort `owner/repo` extraction from a git remote URL, for guides."""
    if not repo_url:
        return "<owner>/<repo>"
    match = _GITHUB_REPO_PATTERN.search(repo_url)
    if not match:
        return "<owner>/<repo>"
    return f"{match.group(1)}/{match.group(2)}"


def open_base_red_ticket(runner: ci_checks.GhRunner, base_branch: str) -> Optional[dict[str, Any]]:
    """Best-effort: file ONE defect ticket for the red `base_branch` of the factory repository.

    Reuses `core.git.autonomy.run_check_main` (PR #81): it probes the last conclusive `DarkFac CI`
    run of the branch, files a `planned` ticket through `run_ticket.py --create --queue-only`
    (idempotent per SHA) and returns the `check-main` payload (`payload["ticket"]["id"]`). Never
    raises: a failed probe or ticket only costs the ticket, never the stage outcome.
    """
    try:
        from core.git import autonomy

        _code, payload = autonomy.run_check_main(
            runner=runner, branch=base_branch, open_ticket=True, notify=False
        )
    except Exception as exc:
        logger.warning("Could not open the defect ticket for the red %s: %s", base_branch, _sanitize(str(exc)))
        return None
    return payload


@dataclass
class GhResult:
    """Normalized outcome of a single `gh` subprocess invocation."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str


def run_gh(
    args: list[str],
    *,
    cwd: Path,
    executable: str = "gh",
    timeout: int = _GH_DEFAULT_TIMEOUT_S,
) -> GhResult:
    """Run one `gh` subcommand as an explicit arg list (never `shell=True`)."""
    argv = [executable, *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise IntegrationError(f"gh {' '.join(args[:2])} timed out after {timeout}s") from exc
    except OSError as exc:
        raise IntegrationError(f"failed to launch gh: {_sanitize(str(exc))}") from exc
    return GhResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=_sanitize(proc.stderr or ""),
    )


class IntegrationStageHandler:
    """StageHandler for the `gh`-CLI GitHub integration stage (HF-27-06)."""

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://line/RunWorkspaceContext",
        output_schema_ref="schema://line/IntegrationOutcome",
        role="integrator",
        required_capabilities=["git_rebase", "gh_pr", "gh_checks"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        project: ProjectDescriptor,
        *,
        gh_executable: str = "gh",
        host_caps: Iterable[str] = (),
        agent_runner: Callable[[AgentRequest], AgentResult] = run_agent,
        routing_config: Optional[RoutingConfig] = None,
        cooldown_path: Optional[Path] = None,
        quota_lookup: Optional[Callable[[str], Optional[float]]] = None,
        ci_check_window_s: int = _CI_CHECK_WINDOW_S,
        gh_timeout_s: int = _GH_DEFAULT_TIMEOUT_S,
        validate_timeout_s: int = _VALIDATE_DEFAULT_TIMEOUT_S,
        base_red_attempts: Optional[BaseRedAttempts] = None,
        base_red_wait_minutes: int = ci_checks.BASE_RED_RETRY_MINUTES,
        base_red_max_retries: int = ci_checks.BASE_RED_MAX_RETRIES,
        human_notifier: Optional[HumanNotifier] = None,
        defect_ticket_func: Optional[DefectTicketFunc] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """`base_red_attempts(run_id)` counts the `base_red` waits this run already spent (the store
        backed counter lives in `core.line.bindings`); without it the job's own iteration is the
        (over-)estimate, which can only shorten the wait, never extend it. `human_notifier` and
        `defect_ticket_func` default to the owner bot and `open_base_red_ticket`; tests inject fakes.
        """
        self.project = project
        self.gh_executable = gh_executable
        self.host_caps = list(host_caps)
        self.agent_runner = agent_runner
        self.routing_config = routing_config
        self.cooldown_path = cooldown_path
        self.quota_lookup = quota_lookup
        self.ci_check_window_s = ci_check_window_s
        self.gh_timeout_s = gh_timeout_s
        self.validate_timeout_s = validate_timeout_s
        self.base_red_attempts = base_red_attempts
        self.base_red_wait_minutes = base_red_wait_minutes
        self.base_red_max_retries = base_red_max_retries
        self.human_notifier = human_notifier
        self.defect_ticket_func = defect_ticket_func
        self.clock = clock

    # ----------------------------------------------------------------
    # git helpers (reuse workspace's auth/sanitization plumbing)
    # ----------------------------------------------------------------

    def _git(
        self, args: list[str], ws: RunWorkspace, *, check: bool = False, timeout: int = 300
    ) -> "subprocess.CompletedProcess[str]":
        repo_url = ws_mod._remote_url(ws.path)
        return ws_mod._run_git(args, cwd=ws.path, repo_url=repo_url, check=check, timeout=timeout)

    def _gh(self, args: list[str], ws: RunWorkspace, *, timeout: Optional[int] = None) -> GhResult:
        return run_gh(
            args, cwd=ws.path, executable=self.gh_executable, timeout=timeout or self.gh_timeout_s
        )

    def _job_key(self, run_id: str, suffix: str) -> str:
        return f"{run_id}:{suffix}"

    # ----------------------------------------------------------------
    # Step 1: checkout, rebase, conflict resolution, push
    # ----------------------------------------------------------------

    def _resolve_conflict(self, ws: RunWorkspace, default_branch: str) -> AgentResult:
        choice = pick(
            "development",
            self.host_caps,
            config=self.routing_config,
            quota_lookup=self.quota_lookup,
            cooldown_path=self.cooldown_path,
        )
        if choice is None:
            return AgentResult(
                ok=False,
                text="no harness available to resolve the merge conflict",
                harness="none",
                duration_s=0.0,
                error_kind="not_installed",
            )
        harness, model = choice
        prompt = (
            f"resolva o merge de origin/{default_branch} nesta branch "
            "preservando ambos os comportamentos"
        )
        req = AgentRequest(prompt=prompt, cwd=ws.path, mode="write", harness=harness, model=model)
        result = self.agent_runner(req)
        record_result(result, config=self.routing_config, cooldown_path=self.cooldown_path)
        return result

    def _run_validate(self, ws: RunWorkspace) -> tuple[bool, str]:
        commands = resolve_commands(self.project, ws.path)
        executable_validate = [cmd for cmd in commands.validate_cmds if cmd.strip()]
        if not executable_validate:
            return False, "Nenhum comando de validate foi detectado apos resolver o conflito."
        logs: list[str] = []
        for cmd in executable_validate:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(ws.path),
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.validate_timeout_s,
                    **_win_kwargs(),
                )
            except subprocess.TimeoutExpired:
                logs.append(f"$ {cmd}\n(timed out after {self.validate_timeout_s}s)")
                return False, _truncate("\n".join(logs), 8000)
            logs.append(f"$ {cmd}\n{proc.stdout}\n{proc.stderr}")
            if proc.returncode != 0:
                return False, _truncate("\n".join(logs), 8000)
        return True, _truncate("\n".join(logs), 4000)

    def _rebase_onto_default(
        self, ws: RunWorkspace, default_branch: str
    ) -> Optional[StageResult]:
        """Fetch + rebase onto `origin/<default>`, resolving conflicts once.

        Returns `None` when the branch ends up rebased (or already was), or
        a terminal/retry `StageResult` when the caller should stop here.
        """
        fetch = self._git(["fetch", "origin", default_branch], ws)
        if fetch.returncode != 0:
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(f"git_fetch_failed: {fetch.stderr.strip()}"),
            )

        upstream = f"origin/{default_branch}"
        # Already up to date: skip the rebase (and the force-push it implies),
        # so CI polling retries do not rewrite the branch on every call.
        if self._git(["merge-base", "--is-ancestor", upstream, "HEAD"], ws).returncode == 0:
            return None

        run_id = ws.run_id
        identity = ws_mod._identity_args(ws.path)
        resolved_key = self._job_key(run_id, _RESOLVED_JOB_SUFFIX)
        if ws_mod.find_commit_by_job(ws, resolved_key) is not None:
            # The branch carries an agent-made merge commit; a rebase would
            # linearize it away and replay the original conflict. Merge instead.
            merge = self._git([*identity, "merge", "--no-edit", upstream], ws)
            if merge.returncode == 0:
                return None
            self._git(["merge", "--abort"], ws)
            return StageResult(outcome="failed", cause_code="merge_conflict")

        rebase = self._git([*identity, "rebase", upstream], ws)
        if rebase.returncode == 0:
            return None

        # Conflict (or another rebase failure): always abort cleanly first.
        self._git(["rebase", "--abort"], ws)

        attempt_key = self._job_key(run_id, _ATTEMPT_JOB_SUFFIX)
        already_attempted = ws_mod.find_commit_by_job(ws, attempt_key) is not None
        if already_attempted:
            return StageResult(outcome="failed", cause_code="merge_conflict")

        # Record and push the attempt marker before invoking the agent so the
        # one-attempt cap survives a crash or a retry picked up by another host.
        ws_mod.write_context(
            ws, "integration_conflict_attempt.marker", f"attempted for {run_id}\n"
        )
        ws_mod.commit(ws, "chore(line): record integration conflict-resolution attempt", attempt_key)
        marker_push = self._push_force_with_lease(ws)
        if marker_push is not None:
            return marker_push

        agent_result = self._resolve_conflict(ws, default_branch)
        if not agent_result.ok:
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(
                    f"merge_conflict_resolution_failed ({agent_result.error_kind}): "
                    f"{_truncate(agent_result.text, 2000)}"
                ),
            )

        validate_ok, validate_log = self._run_validate(ws)
        if not validate_ok:
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(f"merge_conflict_validate_failed:\n{validate_log}"),
            )

        # The marker keeps the commit non-empty even when the agent already
        # committed its merge, so the resolved trailer always lands.
        ws_mod.write_context(
            ws, "integration_conflict_resolved.marker", f"resolved for {run_id}\n"
        )
        ws_mod.commit(ws, "fix(line): resolve merge conflict with origin/" + default_branch, resolved_key)
        if self._git(["merge-base", "--is-ancestor", upstream, "HEAD"], ws).returncode != 0:
            # The agent edited files but never merged origin/<default>.
            return StageResult(outcome="failed", cause_code="merge_conflict")
        return None

    def _push_force_with_lease(self, ws: RunWorkspace) -> Optional[StageResult]:
        if not ws.branch.startswith("df/"):
            raise IntegrationError(f"refusing to force-push non-df branch '{ws.branch}'")
        repo_url = ws_mod._remote_url(ws.path)
        proc = ws_mod._run_git(
            ["push", "--force-with-lease", "-u", "origin", ws.branch],
            cwd=ws.path,
            repo_url=repo_url,
            check=False,
            timeout=300,
        )
        if proc.returncode != 0:
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(f"push_force_with_lease_failed: {(proc.stderr or '').strip()}"),
            )
        return None

    # ----------------------------------------------------------------
    # Step 2: strip run context into the PR body
    # ----------------------------------------------------------------

    def _compose_report_body(self, directory: Path, context: StageContext) -> str:
        parts: list[str] = []
        demand = _read_if_exists(directory / "DEMAND.md")
        if demand:
            parts.append(f"## Demanda\n\n{_truncate(demand, 2000)}")
        spec = _read_if_exists(directory / "SPEC.md")
        if spec:
            parts.append(f"## SPEC\n\n{_truncate(spec, 4000)}")
        grill = _read_if_exists(directory / "GRILL.md")
        if grill:
            parts.append(f"## Grill (premissas)\n\n{_truncate(grill, 2000)}")
        tickets_raw = _read_if_exists(directory / "tickets.json")
        if tickets_raw:
            try:
                tickets = json.loads(tickets_raw)
            except json.JSONDecodeError:
                tickets = []
            lines = [
                f"- **{t.get('id', '?')}**: {t.get('title', '')}"
                for t in tickets
                if isinstance(t, dict)
            ]
            if lines:
                parts.append("## Tickets\n\n" + "\n".join(lines))
            # HF-27-08 item G: tickets.json is stripped from the branch right
            # after this (context is folded into the PR body, never carried
            # past merge), but the release stage still needs each ticket's
            # `smoke` list. Carried as a hidden JSON comment the release
            # stage parses back out of the merged PR's body (see
            # stage_release._fetch_ticket_smoke_entries).
            ticket_smoke = {
                t.get("id", "?"): t.get("smoke")
                for t in tickets
                if isinstance(t, dict) and t.get("smoke")
            }
            if ticket_smoke:
                parts.append(
                    "<!-- darkfac:ticket_smoke:" + json.dumps(ticket_smoke, ensure_ascii=False) + " -->"
                )
        for review in sorted(directory.glob("review-*.md")):
            parts.append(f"## {review.name}\n\n{_truncate(_read_if_exists(review), 2000)}")
        validation_raw = _read_if_exists(directory / "validation.json")
        if validation_raw:
            parts.append(f"## Validacao\n\n```json\n{_truncate(validation_raw, 2000)}\n```")
        if not parts:
            parts.append(f"(sem contexto adicional do run `{context.claim.job_key.run_id}`)")
        return "\n\n".join(parts)

    def _derive_title(self, directory: Path, run_id: str) -> str:
        for name in ("SPEC.md", "DEMAND.md"):
            text = _read_if_exists(directory / name)
            for line in text.splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    return _truncate(stripped, 120)
        return f"DarkFac run {run_id}"

    def _restore_context(self, ws: RunWorkspace, strip_sha: str, dest: Path) -> Optional[Path]:
        """Copy `.darkfac/runs/<run_id>/` as of `<strip_sha>^` into `dest`."""
        prefix = f".darkfac/runs/{ws.run_id}/"
        listing = self._git(["ls-tree", "-r", "--name-only", f"{strip_sha}^", "--", prefix], ws)
        names = [n for n in (listing.stdout or "").splitlines() if n.startswith(prefix)]
        if listing.returncode != 0 or not names:
            return None
        for name in names:
            shown = safe_show(
                f"{strip_sha}^", name, cwd=ws.path,
                runner=lambda args, _directory: self._git(list(args), ws),
            )
            if shown.returncode != 0:
                continue
            target = dest / name[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(shown.stdout or "", encoding="utf-8")
        return dest

    def _strip_context(
        self, ws: RunWorkspace, context: StageContext
    ) -> tuple[str, str, Optional[StageResult]]:
        """Fold `.darkfac/runs/<run_id>/` into a PR title/body, then remove it.

        Returns `(title, body, error)`. `error` is set when the strip/push
        itself failed; `title`/`body` are always usable (falling back to
        generic text when the context was already stripped by a previous
        call).
        """
        run_id = ws.run_id
        strip_key = self._job_key(run_id, _STRIP_JOB_SUFFIX)
        directory = ws_mod.context_dir(ws)

        strip_sha = ws_mod.find_commit_by_job(ws, strip_key)
        if strip_sha is not None and not directory.exists():
            # Already stripped by an earlier call (e.g. `gh pr create` failed
            # afterwards): rebuild title/body from the strip commit's parent.
            with tempfile.TemporaryDirectory() as tmp:
                restored = self._restore_context(ws, strip_sha, Path(tmp))
                if restored is None:
                    return (
                        f"DarkFac run {run_id}",
                        "(contexto do run ja removido em uma tentativa anterior desta integracao)",
                        None,
                    )
                return (
                    self._derive_title(restored, run_id),
                    self._compose_report_body(restored, context),
                    None,
                )

        title = self._derive_title(directory, run_id) if directory.exists() else f"DarkFac run {run_id}"
        body = (
            self._compose_report_body(directory, context)
            if directory.exists()
            else f"(sem contexto adicional do run `{run_id}`)"
        )
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        # A CI fix-up restores the context after the first strip. Strip it on
        # every new integration pass so the final PR does not carry run state.
        ws_mod.commit(
            ws, "chore(line): drop run context (details folded into PR body)",
            strip_key if strip_sha is None else self._job_key(
                run_id, f"integration:strip_context:{context.claim.job_key.iteration}"
            ),
        )

        push_error = self._push_force_with_lease(ws)
        if push_error is not None:
            return title, body, push_error
        return title, body, None

    # ----------------------------------------------------------------
    # Step 3: idempotent PR lookup/creation
    # ----------------------------------------------------------------

    def _find_open_pr(self, ws: RunWorkspace) -> Optional[dict[str, Any]]:
        result = self._gh(["pr", "list", "--head", ws.branch, "--json", "number,url,state"], ws)
        if not result.ok:
            return None
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        for item in items:
            if isinstance(item, dict) and item.get("state") == "OPEN":
                return item
        return None

    def _find_merged_pr(self, ws: RunWorkspace) -> Optional[dict[str, Any]]:
        result = self._gh(
            [
                "pr", "list", "--head", ws.branch, "--state", "merged",
                "--json", "number,url,state,mergeCommit",
            ],
            ws,
        )
        if not result.ok:
            return None
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        for item in items:
            if isinstance(item, dict) and item.get("state") == "MERGED":
                return item
        return None

    def _create_pr(
        self, ws: RunWorkspace, default_branch: str, title: str, body: str
    ) -> Optional[dict[str, Any]]:
        body_path = ws.path.parent / f"REPORT_PR_{ws.run_id}.md"
        try:
            body_path.write_text(body, encoding="utf-8")
            result = self._gh(
                [
                    "pr", "create",
                    "--base", default_branch,
                    "--head", ws.branch,
                    "--title", title,
                    "--body-file", str(body_path),
                ],
                ws,
                timeout=120,
            )
        finally:
            try:
                body_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not result.ok:
            logger.error("gh pr create failed for %s: %s", ws.branch, result.stderr)
            return None
        url = ""
        for line in reversed(result.stdout.strip().splitlines()):
            if line.strip().startswith("http"):
                url = line.strip()
                break
        number = None
        match = re.search(r"/pull/(\d+)", url)
        if match:
            number = int(match.group(1))
        if number is None:
            # Fall back to a fresh lookup (covers `gh` versions with different stdout).
            return self._find_open_pr(ws)
        return {"number": number, "url": url, "state": "OPEN"}

    def _find_or_create_pr(
        self, ws: RunWorkspace, default_branch: str, title: str, body: str
    ) -> Optional[dict[str, Any]]:
        existing = self._find_open_pr(ws)
        if existing is not None:
            return existing
        return self._create_pr(ws, default_branch, title, body)

    # ----------------------------------------------------------------
    # Step 4/5: CI checks
    # ----------------------------------------------------------------

    def _ci_runner(self, ws: RunWorkspace, *, timeout: Optional[int] = None) -> ci_checks.GhRunner:
        """Adapt `_gh` to the runner protocol of `core.git.ci_checks`."""

        def _run(args: Sequence[str], _cwd: Path) -> GhResult:
            return self._gh(list(args), ws, timeout=timeout)

        return _run

    def _ci_failure_count(self, ws: RunWorkspace) -> int:
        """Count recorded red PR checks in the run's branch history, across strips."""
        result = self._git(["log", "--format=%B"], ws)
        if result.returncode != 0:
            raise IntegrationError("cannot count CI failures from branch history")
        prefix = re.escape(f"DarkFac-Job: {ws.run_id}:ci-failure:")
        return len(re.findall(rf"^{prefix}\d+$", result.stdout or "", re.MULTILINE))

    def _record_ci_failure(
        self, ws: RunWorkspace, failing: dict[str, Any], log: str, context: StageContext
    ) -> StageResult:
        """Persist feedback on the run branch before bouncing to development."""
        count = self._ci_failure_count(ws)
        if count >= MAX_CI_ITERATIONS:
            return StageResult(outcome="failed", cause_code="ci_iteration_cap")
        directory = ws_mod.context_dir(ws)
        if not (directory / "tickets.json").is_file():
            # On later rounds use the strip immediately preceding this CI
            # check, preserving review state and earlier CI feedback.
            current_strip_key = self._job_key(
                ws.run_id, f"integration:strip_context:{context.claim.job_key.iteration}"
            )
            strip_sha = ws_mod.find_commit_by_job(ws, current_strip_key)
            if strip_sha is None:
                strip_sha = ws_mod.find_commit_by_job(
                    ws, self._job_key(ws.run_id, _STRIP_JOB_SUFFIX)
                )
            if strip_sha is None or self._restore_context(ws, strip_sha, directory) is None:
                return StageResult(outcome="failed", cause_code="ci_context_unavailable")
        number = count + 1
        label = _sanitize(ci_checks.check_label(failing))
        content = f"# CI failure {number}: {label}\n\n{_truncate(_sanitize(redact_secrets(log)), 4000)}\n"
        path = ws_mod.write_context(ws, f"ci-{number}.log.md", content)
        ws_mod.commit(
            ws, f"chore(line): record CI failure {number}",
            self._job_key(ws.run_id, f"ci-failure:{context.claim.job_key.iteration}"),
        )
        ws_mod.push(ws)
        return StageResult(
            outcome="retry", cause_code="retry:development",
            evidence_refs=[str(path.relative_to(ws.path))],
        )

    def _ci_grace_path(self, ws: RunWorkspace) -> Optional[Path]:
        """Return per-worktree Git metadata storage, outside the candidate tree."""
        result = self._git(["rev-parse", "--git-path", "darkfac-ci-grace.json"], ws)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        path = Path(result.stdout.strip())
        return path if path.is_absolute() else ws.path / path

    def _no_checks_seen_at(
        self, path: Optional[Path], pr_number: int, head_sha: str
    ) -> Optional[datetime]:
        """Read the first empty snapshot for this PR and exact candidate head."""
        if path is None:
            return None
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
            if marker.get("pr_number") != pr_number or marker.get("head_sha") != head_sha:
                return None
            seen_at = datetime.fromisoformat(marker["seen_at"])
            return seen_at if seen_at.tzinfo is not None else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _remember_no_checks(
        self, path: Optional[Path], pr_number: int, head_sha: str, now: datetime
    ) -> bool:
        """Persist the grace start so a new handler can resume after a retry."""
        if path is None:
            return False
        marker = {"pr_number": pr_number, "head_sha": head_sha, "seen_at": now.isoformat()}
        try:
            path.write_text(json.dumps(marker), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist CI grace start: %s", _sanitize(str(exc)))
            return False
        return True

    def _evaluate_checks(
        self, ws: RunWorkspace, pr_number: int, context: Optional[StageContext] = None
    ) -> Optional[StageResult]:
        """Evaluate one CI snapshot and defer every wait to the job scheduler."""
        head = self._git(["rev-parse", "HEAD"], ws)
        if head.returncode != 0 or not head.stdout.strip():
            return StageResult(outcome="retry", cause_code="ci_head_unavailable")
        head_sha = head.stdout.strip()
        now = self.clock()
        grace = ci_checks.resolve_no_checks_grace_seconds()
        grace_path = self._ci_grace_path(ws)
        first_seen = self._no_checks_seen_at(grace_path, pr_number, head_sha)
        elapsed = max(0.0, (now - first_seen).total_seconds()) if first_seen else 0.0
        verdict = ci_checks.ensure_green(
            pr_number,
            ws.path,
            self._ci_runner(ws),
            timeout_s=0,
            no_checks_grace_s=0 if first_seen is not None and elapsed >= grace else grace,
            base_branch=self.project.default_branch or "main",
            expected_head_sha=head_sha,
            require_no_workflows=True,
        )
        if verdict.status == "failed":
            snapshot = verdict.snapshot
            assert snapshot is not None and snapshot.failing
            if verdict.base_red:
                return self._base_red_outcome(
                    ws, pr_number, snapshot, self.project.default_branch or "main", context
                )
            failing = snapshot.failing[0]
            if context is None:
                return StageResult(outcome="failed", cause_code="ci_context_unavailable")
            return self._record_ci_failure(ws, failing, verdict.log_tail, context)
        if verdict.status == "pending_timeout":
            wait_s = self.ci_check_window_s
            if verdict.snapshot is not None and verdict.snapshot.state == "no_checks":
                if first_seen is None:
                    if not self._remember_no_checks(grace_path, pr_number, head_sha, now):
                        logger.warning("Cannot establish no-checks grace for PR #%s", pr_number)
                    elapsed = 0.0
                if elapsed < grace:
                    remaining = grace - elapsed
                    wait_s = min(wait_s, remaining) if wait_s > 0 else remaining
            not_before = (now + timedelta(seconds=wait_s)).isoformat()
            return StageResult(outcome="retry", cause_code=f"ci_pending:not_before={not_before}")
        if not verdict.passed:
            return StageResult(outcome="retry", cause_code="ci_unverified")
        return None

    # ----------------------------------------------------------------
    # base_red: every red check is already red on the base branch (USR-86)
    # ----------------------------------------------------------------

    def _base_red_waits_spent(self, ws: RunWorkspace, context: Optional[StageContext]) -> int:
        """`base_red` waits this run already returned (store counter, else the job iteration)."""
        if self.base_red_attempts is not None:
            try:
                return max(int(self.base_red_attempts(ws.run_id)), 0)
            except Exception as exc:  # a counter failure must not turn into an endless wait
                logger.warning("base_red attempt counter failed for %s: %s", ws.run_id, _sanitize(str(exc)))
        # Every earlier retry of this stage bumped the iteration, so it never undercounts.
        return context.claim.job_key.iteration if context is not None else 0

    def _base_red_outcome(
        self,
        ws: RunWorkspace,
        pr_number: int,
        snapshot: ci_checks.CheckSnapshot,
        base_branch: str,
        context: Optional[StageContext],
    ) -> StageResult:
        """Wait for a red base instead of re-iterating development (no agent call is made here).

        `retry("base_red not_before=<now + base_red_wait_minutes>")` while the run still has waits
        left; once they are spent, `waiting_human(infra)`.
        """
        labels = ", ".join(ci_checks.check_label(c) for c in snapshot.failing)
        spent = self._base_red_waits_spent(ws, context)
        if spent < self.base_red_max_retries:
            not_before = self.clock() + timedelta(minutes=self.base_red_wait_minutes)
            logger.warning(
                "PR #%s is red only where %s is already red (%s): waiting %d min (wait %d/%d) "
                "instead of re-iterating development",
                pr_number, base_branch, labels, self.base_red_wait_minutes,
                spent + 1, self.base_red_max_retries,
            )
            return StageResult(outcome="retry", cause_code=ci_checks.base_red_cause_code(not_before))

        logger.warning(
            "PR #%s: %s is still red after %d waits (%s); escalating to the owner",
            pr_number, base_branch, spent, labels,
        )
        ticket_id = self._open_base_red_ticket(ws, base_branch)
        probe = (
            _FACTORY_BASE_PROBE.format(branch=base_branch)
            if self.project.id == _FACTORY_PROJECT_ID
            else None
        )
        request = HumanRequest(
            kind="infra",
            run_id=ws.run_id,
            blocking_stage=STAGE,
            guide_md=self._base_red_guide(pr_number, snapshot.failing, base_branch, ticket_id),
            probe_cmd=probe,
        )
        self._notify_human(request)
        return StageResult(outcome="waiting_human", cause_code="base_red_exhausted")

    def _open_base_red_ticket(self, ws: RunWorkspace, base_branch: str) -> Optional[str]:
        """`darkfac` only: best-effort defect ticket for its own red base. Never raises."""
        if self.project.id != _FACTORY_PROJECT_ID:
            return None
        opener = self.defect_ticket_func or open_base_red_ticket
        try:
            payload = opener(self._ci_runner(ws), base_branch)
        except Exception as exc:
            logger.warning("Defect ticket for the red %s not opened: %s", base_branch, _sanitize(str(exc)))
            return None
        ticket = (payload or {}).get("ticket")
        ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        return str(ticket_id) if ticket_id else None

    def _notify_human(self, request: HumanRequest) -> None:
        """Owner message for the request. Not persisted on the branch: the run context is already
        stripped from it (step 2), and writing it back would leak into the merge. Never raises."""
        notifier = self.human_notifier or notify_human_request
        try:
            notifier(request)
        except Exception as exc:
            logger.warning("Owner notification for run %s failed: %s", request.run_id, _sanitize(str(exc)))

    def _base_red_guide(
        self,
        pr_number: int,
        failing: Sequence[dict[str, Any]],
        base_branch: str,
        ticket_id: Optional[str],
    ) -> str:
        repo = owner_repo(self.project.repo_url)
        waited_h = self.base_red_wait_minutes * self.base_red_max_retries / 60
        checks = "\n".join(
            f"   - {ci_checks.check_label(c)}: {c.get('link') or '(sem link)'}" for c in failing
        )
        steps = [
            f"Abra https://github.com/{repo}/actions?query=branch%3A{base_branch}, logado com "
            "acesso ao repositorio.",
            "Clique no run mais recente com a marca vermelha e abra o job vermelho para ler o log "
            "e achar a causa.",
            "Se a falha for passageira (runner indisponivel, limite do GitHub Actions), clique em "
            "'Re-run all jobs' no canto superior direito do run.",
            f"Se for defeito de codigo ou de configuracao, corrija na branch '{base_branch}' (ou "
            f"abra um PR de correcao); o PR #{pr_number} da fabrica fica aberto esperando.",
        ]
        if self.project.id == _FACTORY_PROJECT_ID:
            steps.append(
                f"A fabrica abriu o ticket {ticket_id} na fila de desenvolvimento para corrigir a base."
                if ticket_id
                else "A fabrica nao conseguiu abrir o ticket de defeito da base sozinha; registre-o "
                "na fila de desenvolvimento com o link do run vermelho."
            )
        steps.append(
            f"Nao precisa fazer mais nada depois que o run da branch '{base_branch}' ficar verde: "
            f"a fabrica retoma sozinha este run, atualiza o PR #{pr_number} com a base corrigida "
            "e conclui o merge."
        )
        numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(steps, start=1))
        return (
            f"kind=infra: o CI da branch base '{base_branch}' ({repo}) ja estava vermelho nos mesmos "
            f"checks que reprovaram o PR #{pr_number}. Nenhuma mudanca do agente resolve isso, entao a "
            f"fabrica parou de re-iterar o desenvolvimento e esperou {waited_h:g}h "
            f"({self.base_red_max_retries} verificacoes de {self.base_red_wait_minutes} min) a base "
            "voltar a ficar verde, sem sucesso.\n"
            f"Checks vermelhos (link do check no PR):\n{checks}\n"
            f"Passo a passo (GitHub, interface atual):\n{numbered}"
        )

    # ----------------------------------------------------------------
    # Step 6/7: merge, human fallback, ancestry confirmation
    # ----------------------------------------------------------------

    def _human_request_branch_protection(self, pr_number: int) -> StageResult:
        repo = owner_repo(self.project.repo_url)
        guide = (
            "kind=repo_setting: a branch protegida exige revisao humana antes do merge "
            "automatico. Passo a passo (GitHub, interface atual):\n"
            f"1. Abra https://github.com/{repo}/settings/branches, logado como "
            "administrador do repositorio.\n"
            "2. Em 'Branch protection rules', clique em 'Edit' na regra da branch padrao.\n"
            "3. Em 'Require a pull request before merging', reduza "
            "'Required number of approvals before merging' para 1 (ou desmarque a opcao "
            "inteira, se este repositorio nao exigir mais revisao humana obrigatoria para "
            "merges automatizados da fabrica).\n"
            "4. Se 'Require review from Code Owners' estiver marcada, desmarque-a ou "
            "adicione a conta de servico da fabrica ao arquivo CODEOWNERS.\n"
            "5. Clique em 'Save changes' no fim da pagina.\n"
            f"6. Alternativa sem mudar a politica: aprove manualmente o PR #{pr_number} em "
            f"https://github.com/{repo}/pull/{pr_number} ('Review changes' -> 'Approve' -> "
            "'Submit review'); a fabrica detecta a aprovacao e reenfileira o merge."
        )
        return StageResult(outcome="waiting_human", cause_code=guide)

    def _merge_pr(self, ws: RunWorkspace, pr_number: int) -> tuple[Optional[str], Optional[StageResult]]:
        merge_result = self._gh(
            ["pr", "merge", str(pr_number), "--squash", "--delete-branch"], ws, timeout=120
        )
        if not merge_result.ok:
            combined = f"{merge_result.stdout}\n{merge_result.stderr}".lower()
            blocked_terms = ("review", "protected branch", "required status", "not mergeable")
            if any(term in combined for term in blocked_terms):
                auto_result = self._gh(
                    ["pr", "merge", str(pr_number), "--squash", "--delete-branch", "--auto"],
                    ws,
                    timeout=120,
                )
                if not auto_result.ok:
                    return None, self._human_request_branch_protection(pr_number)
            else:
                return None, StageResult(
                    outcome="failed",
                    cause_code=_sanitize(f"gh_pr_merge_failed: {_truncate(merge_result.stderr, 2000)}"),
                )

        view_result = self._gh(
            ["pr", "view", str(pr_number), "--json", "mergeCommit,state,url"], ws
        )
        if view_result.ok:
            try:
                data = json.loads(view_result.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            merge_commit = data.get("mergeCommit") or {}
            sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            if sha:
                return sha, None
        # `--auto` may have queued the merge without completing it yet.
        return None, StageResult(outcome="retry", cause_code="merge_queued_awaiting_confirmation")

    def _confirm_remote_ancestry(
        self, ws: RunWorkspace, default_branch: str, merge_sha: str
    ) -> Optional[StageResult]:
        self._git(["fetch", "origin", default_branch], ws)
        check = self._git(
            ["merge-base", "--is-ancestor", merge_sha, f"origin/{default_branch}"], ws
        )
        if check.returncode != 0:
            return StageResult(outcome="retry", cause_code="merge_not_yet_on_remote_default")
        return None

    # ----------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------

    def handle(self, context: StageContext) -> StageResult:
        run_id = context.claim.job_key.run_id
        default_branch = self.project.default_branch or "main"

        try:
            ws = ws_mod.checkout(self.project, run_id)
        except WorkspaceError as exc:
            return StageResult(outcome="retry", cause_code=_sanitize(f"workspace_checkout_failed: {exc}"))

        # Replaying a claim after publishing its feedback must not poll the
        # same red SHA or create another CI iteration.
        if ws_mod.find_commit_by_job(
            ws, self._job_key(run_id, f"ci-failure:{context.claim.job_key.iteration}")
        ) is not None:
            ws_mod.push(ws)
            return StageResult(outcome="retry", cause_code="retry:development")

        # Idempotency: a previous call may have merged (or queued `--auto`)
        # and deleted the branch before returning; never redo the work.
        merged = self._find_merged_pr(ws)
        if merged is not None:
            merged_sha = (merged.get("mergeCommit") or {}).get("oid")
            if merged_sha:
                ancestry_error = self._confirm_remote_ancestry(ws, default_branch, merged_sha)
                if ancestry_error is not None:
                    return ancestry_error
                return StageResult(outcome="success", output_refs=[merged.get("url") or "", merged_sha])

        rebase_result = self._rebase_onto_default(ws, default_branch)
        if rebase_result is not None:
            return rebase_result

        push_result = self._push_force_with_lease(ws)
        if push_result is not None:
            return push_result

        title, body, strip_error = self._strip_context(ws, context)
        if strip_error is not None:
            return strip_error

        pr = self._find_or_create_pr(ws, default_branch, title, body)
        if pr is None:
            return StageResult(outcome="failed", cause_code="gh_pr_create_failed")

        pr_number = pr.get("number")
        if not isinstance(pr_number, int):
            return StageResult(outcome="failed", cause_code="gh_pr_missing_number")

        ci_result = self._evaluate_checks(ws, pr_number, context)
        if ci_result is not None:
            return ci_result

        merge_sha, merge_error = self._merge_pr(ws, pr_number)
        if merge_error is not None:
            return merge_error
        assert merge_sha is not None

        ancestry_error = self._confirm_remote_ancestry(ws, default_branch, merge_sha)
        if ancestry_error is not None:
            return ancestry_error

        pr_url = pr.get("url") or ""
        return StageResult(outcome="success", output_refs=[pr_url, merge_sha])


__all__ = [
    "IntegrationStageHandler",
    "IntegrationError",
    "GhResult",
    "run_gh",
    "owner_repo",
    "open_base_red_ticket",
]
