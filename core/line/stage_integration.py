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
4. Poll CI (`gh pr checks --json`); pending checks return `retry` with a
   `not_before` hint rather than blocking the stage for 30 minutes.
5. A red check downloads the failing job's log and returns `retry` (for the
   `development` stage) with that log attached.
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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import RoutingConfig, pick, record_result
from core.line.workspace import RunWorkspace, WorkspaceError
from core.projects.models import ProjectDescriptor
from core.projects.registry import resolve_commands
from core.workflow.control_contracts import HandlerDescriptor, StageContext, StageResult

logger = logging.getLogger(__name__)

STAGE = "integration"
VERSION = "v1"

# One 5-minute polling window per stage invocation; the caller is expected to
# re-invoke the stage (respecting `not_before`) until CI resolves or the
# run's overall wall-clock cap (`run_caps.wall_clock_hours`) is hit.
_CI_CHECK_WINDOW_S = 300
_GH_DEFAULT_TIMEOUT_S = 60
_VALIDATE_DEFAULT_TIMEOUT_S = 1800

_ATTEMPT_JOB_SUFFIX = "integration:conflict_attempt"
_RESOLVED_JOB_SUFFIX = "integration:conflict_resolved"
_STRIP_JOB_SUFFIX = "integration:strip_context"

_TOKEN_LIKE = re.compile(
    r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]+", re.IGNORECASE
)
_GITHUB_REPO_PATTERN = re.compile(
    r"github\.com[:/]+([^/]+)/([^/.\s]+?)(?:\.git)?/?$", re.IGNORECASE
)
_RUN_LINK_PATTERN = re.compile(r"/runs/(\d+)")


class IntegrationError(RuntimeError):
    """Raised for unrecoverable gh/git plumbing errors inside this stage."""


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _sanitize(text: str) -> str:
    return _TOKEN_LIKE.sub("[REDACTED_TOKEN]", text or "")


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
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
        self.sleep = sleep

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
        if not commands.validate_cmds:
            return True, "(sem comandos de validate configurados)"
        logs: list[str] = []
        for cmd in commands.validate_cmds:
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
            shown = self._git(["show", f"{strip_sha}^:{name}"], ws)
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
        if strip_sha is not None:
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
        ws_mod.commit(ws, "chore(line): drop run context (details folded into PR body)", strip_key)

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

    def _fetch_failed_log(self, ws: RunWorkspace, failing_check: dict[str, Any]) -> str:
        link = str(failing_check.get("link") or "")
        match = _RUN_LINK_PATTERN.search(link)
        if not match:
            return "(nao foi possivel localizar o run id do check falho)"
        gh_run_id = match.group(1)
        result = self._gh(["run", "view", gh_run_id, "--log-failed"], ws, timeout=120)
        text = result.stdout if result.stdout.strip() else result.stderr
        lines = text.splitlines()
        return "\n".join(lines[-150:])

    def _evaluate_checks(self, ws: RunWorkspace, pr_number: int) -> Optional[StageResult]:
        """Return a terminal/retry `StageResult` unless every check is green.

        `None` means "no checks configured, or all green" — proceed to merge,
        matching "se o repo nao tiver checks, a validacao limpa do HF-27-05 e
        o gate".
        """
        result = self._gh(
            ["pr", "checks", str(pr_number), "--json", "bucket,name,link,workflow"], ws
        )
        if not result.ok:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "no checks reported" in combined or "no commit found" in combined:
                return None
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(f"gh_pr_checks_failed: {_truncate(result.stderr, 2000)}"),
            )
        try:
            checks = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            checks = []
        if not checks:
            return None

        failing = [c for c in checks if isinstance(c, dict) and c.get("bucket") == "fail"]
        if failing:
            log = self._fetch_failed_log(ws, failing[0])
            return StageResult(
                outcome="retry",
                cause_code=_sanitize(
                    f"ci_check_failed:{failing[0].get('name')}\n{_truncate(log, 4000)}"
                ),
            )

        pending = [
            c
            for c in checks
            if isinstance(c, dict) and c.get("bucket") not in ("pass", "skipping", "cancel")
        ]
        if pending:
            not_before = (
                datetime.now(timezone.utc) + timedelta(seconds=self.ci_check_window_s)
            ).isoformat()
            return StageResult(outcome="retry", cause_code=f"ci_pending:not_before={not_before}")

        return None

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

        ci_result = self._evaluate_checks(ws, pr_number)
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
]
