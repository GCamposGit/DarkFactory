"""Release stage for the DarkFac production line (HF-27-07).

Publishes the SHA merged by `IntegrationStageHandler` (HF-27-06) to the
project's configured deploy target, proves it is live with smoke checks, and
rolls back automatically when it is not, per
`docs/handoffs/production-line/HF-27-07.md`.

Input contract (documented here because HF-27-08's DAG wiring, which decides
how one stage's `output_refs` become the next stage's `input_refs`, has not
landed yet): `context.input_refs` is expected to hold
`[pr_url, merge_sha]` — exactly the shape of
`IntegrationStageHandler`'s own `output_refs` — with `merge_sha` as the last
element. A missing or malformed `merge_sha` is a terminal `failed`, never a
guess.

Flow (`ReleaseStageHandler.handle`):

1. **Idempotency** — if `merge_sha` was already recorded as the project's
   `last_good_sha` by a previous call, reuse that recorded outcome instead of
   deploying again.
2. **Commercial acceptance gate** — `project.requires_commercial_acceptance`
   pauses in `waiting_human(kind=commercial_acceptance)` with the PR link and
   (when configured) a preview URL, before touching the deploy target. Only
   `record_commercial_acceptance()` (called by the human channel) opens it.
3. **Deploy** — dispatches to the adapter matching `project.deploy.type`:
   - `dokploy`: `DokployDeploymentAdapter`, reconciled via
     `core.orchestrator.deployment_adapter.execute_deployment_with_safeguards`-style
     polling (kept inline here since that helper does not expose a smoke hook
     matching this stage's retry/rollback contract).
   - `hostinger_ftp`: `FtpMirrorDeploymentAdapter` (new in
     `core.orchestrator.deployment_adapter`).
   - `local_service`: `LocalServiceDeploymentAdapter`, running
     `deploy.params.restart_cmd` first. Claiming a `local_service` job
     without the `target:local_service` capability is prevented upstream by
     `ControlStore.claim()`'s `required_capabilities` filter (see
     `tests/line/test_stage_release.py::test_local_service_claim_requires_capability`);
     this handler does not re-check host capabilities itself.
   - `none` (or no `deploy` configured): no remote deploy; smoke runs the
     project's local `commands.smoke` instead of HTTP checks.
4. **Smoke** — for `dokploy`/`hostinger_ftp`/`local_service`, each
   `project.smoke` `SmokeCheck` is retried up to 5 times (10s apart);
   `expect_status`/`expect_text` must match. A `healthcheck_endpoint` (from
   `deploy.params`) reporting a mismatched SHA also fails the smoke.
5. **Automatic rollback** — a failed smoke on a project that has a recorded
   `last_good_sha` triggers `adapter.rollback()` to it; the stage then
   returns `retry` carrying the smoke log, so the caller (HF-27-08's DAG) can
   route it back to `development` for "corrigir regressao em producao". A
   second consecutive smoke failure for the same `run_id` is terminal
   (`failed(prod_smoke_failed)`) instead of retrying forever.
6. **State** — `last_good_sha`, the per-run smoke-failure streak and the
   commercial acceptance must outlive the `df/<run_id>` branch (deleted on
   merge) and be visible to every host, so `GitRefReleaseStateStore` keeps
   them as `refs/darkfac/release/*` in the target repo. The JSON-file
   `ReleaseStateStore` is host-local and only for tests or repo-less projects.
   A rollback is verified by smoking the restored SHA; if that fails the
   stage returns `failed` (production needs a human), never another retry.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, Field

from core.line import workspace as ws_mod
from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.deployment_adapter import (
    DeploymentAdapter,
    DeploymentOperation,
    DeploymentStatus,
    DokployDeploymentAdapter,
    FtpMirrorDeploymentAdapter,
    LocalServiceDeploymentAdapter,
    TargetConfig,
)
from core.projects.models import DeployTargetType, ProjectDescriptor, SmokeCheck
from core.projects.registry import normalize_repo_url, resolve_commands
from core.workflow.control_contracts import HandlerDescriptor, StageContext, StageResult

logger = logging.getLogger(__name__)

STAGE = "release"
VERSION = "v1"

_SMOKE_RETRIES = 5
_SMOKE_DELAY_S = 10.0
_RECONCILE_POLLS = 30
_RECONCILE_DELAY_S = 1.0
_MAX_CONSECUTIVE_SMOKE_FAILURES = 2

# HF-27-08 item G: PlanningTicket.smoke entries embedded by stage_integration
# as a hidden JSON comment in the PR body (tickets.json itself never
# survives the merge -- stage_integration strips `.darkfac/runs/<run_id>/`
# right after folding it into the PR body).
_TICKET_SMOKE_COMMENT_RE = re.compile(r"<!--\s*darkfac:ticket_smoke:(?P<json>.*?)-->", re.DOTALL)
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


class ReleaseError(RuntimeError):
    """Raised for unrecoverable plumbing errors inside this stage."""


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


class ReleaseRunState(BaseModel):
    """Per-run bookkeeping needed for the two-strikes rollback rule."""

    last_sha: str = ""
    consecutive_smoke_failures: int = 0
    commercial_accepted_sha: Optional[str] = None


class ReleaseState(BaseModel):
    """Project-level release state persisted across runs (see module docstring)."""

    last_good_sha: Optional[str] = None
    last_success_operation_id: Optional[str] = None
    last_success_smoke_ref: Optional[str] = None
    runs: Dict[str, ReleaseRunState] = Field(default_factory=dict)


class ReleaseStateStore:
    """Loads/saves `ReleaseState` as JSON, one file per project.

    Host-local: only for tests and projects without a `repo_url`. Hosts share
    work (VPS, Desktop, Notebook), so production uses `GitRefReleaseStateStore`.
    """

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self.state_dir = state_dir or (ws_mod.workspace_root() / "_release_state")

    def _path(self, project_id: str) -> Path:
        return self.state_dir / f"{project_id}.json"

    def load(self, project_id: str) -> ReleaseState:
        path = self._path(project_id)
        if not path.is_file():
            return ReleaseState()
        try:
            return ReleaseState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("release state for %s is unreadable; starting fresh", project_id)
            return ReleaseState()

    def save(self, project_id: str, state: ReleaseState) -> None:
        path = self._path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


_REF_ROOT = "refs/darkfac/release"


class GitRefReleaseStateStore:
    """`ReleaseState` kept as refs in the target repo, shared by every host.

    - `refs/darkfac/release/last-good` -> the last SHA that passed smoke.
    - `refs/darkfac/release/runs/<run_id>/smoke-fail-<n>` -> each failed SHA.
    - `refs/darkfac/release/runs/<run_id>/accepted` -> commercially accepted SHA.

    Custom refs are not branches or tags, so they stay out of clones and the
    GitHub UI. Every value is a commit SHA already on the remote.
    """

    def __init__(self, project: ProjectDescriptor) -> None:
        if not project.repo_url:
            raise ReleaseError(f"project '{project.id}' has no repo_url for release state")
        self.project = project
        self.repo_url = normalize_repo_url(project.repo_url)

    def _mirror(self) -> Path:
        root = ws_mod.workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        return ws_mod._ensure_mirror(self.project.id, self.repo_url, root)

    def _remote_refs(self, mirror: Path) -> dict[str, str]:
        proc = ws_mod._run_git(
            ["ls-remote", "origin", f"{_REF_ROOT}/*"], cwd=mirror, repo_url=self.repo_url
        )
        refs: dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            sha, _, name = line.partition("\t")
            if name:
                refs[name.strip()] = sha.strip()
        return refs

    def _push(self, mirror: Path, updates: dict[str, str]) -> None:
        if not updates:
            return
        specs = [f"+{sha}:{ref}" for ref, sha in updates.items()]
        ws_mod._run_git(["push", "origin", *specs], cwd=mirror, repo_url=self.repo_url)

    def load(self, project_id: str) -> ReleaseState:
        refs = self._remote_refs(self._mirror())
        state = ReleaseState(last_good_sha=refs.get(f"{_REF_ROOT}/last-good"))
        prefix = f"{_REF_ROOT}/runs/"
        for name, sha in refs.items():
            if not name.startswith(prefix):
                continue
            run_id, _, leaf = name[len(prefix):].rpartition("/")
            run_state = state.runs.setdefault(run_id, ReleaseRunState())
            if leaf.startswith("smoke-fail-"):
                run_state.consecutive_smoke_failures += 1
                run_state.last_sha = sha
            elif leaf == "accepted":
                run_state.commercial_accepted_sha = sha
        return state

    def save(self, project_id: str, state: ReleaseState) -> None:
        mirror = self._mirror()
        current = self._remote_refs(mirror)
        updates: dict[str, str] = {}
        if state.last_good_sha and current.get(f"{_REF_ROOT}/last-good") != state.last_good_sha:
            updates[f"{_REF_ROOT}/last-good"] = state.last_good_sha
        for run_id, run_state in state.runs.items():
            base = f"{_REF_ROOT}/runs/{run_id}"
            for n in range(1, run_state.consecutive_smoke_failures + 1):
                ref = f"{base}/smoke-fail-{n}"
                if ref not in current and run_state.last_sha:
                    updates[ref] = run_state.last_sha
            accepted = run_state.commercial_accepted_sha
            if accepted and current.get(f"{base}/accepted") != accepted:
                updates[f"{base}/accepted"] = accepted
        self._push(mirror, updates)


def default_state_store(project: ProjectDescriptor) -> "ReleaseStateStore | GitRefReleaseStateStore":
    """Shared git-ref store when the project has a repo; host-local file otherwise."""
    if project.repo_url:
        return GitRefReleaseStateStore(project)
    logger.warning("project %s has no repo_url; release state is host-local", project.id)
    return ReleaseStateStore()


def record_commercial_acceptance(
    store: "ReleaseStateStore | GitRefReleaseStateStore", project_id: str, run_id: str, sha: str
) -> None:
    """Record the owner's commercial acceptance of `sha` for `run_id`.

    Called by the human channel (HF-27-08) when the owner approves; the
    release stage never records acceptance on its own.
    """
    state = store.load(project_id)
    state.runs.setdefault(run_id, ReleaseRunState()).commercial_accepted_sha = sha
    store.save(project_id, state)


def _run_smoke_http(
    check: SmokeCheck,
    *,
    retries: int,
    delay_s: float,
    sleep: Callable[[float], None],
    opener: Callable[[str, float], tuple[int, bytes]],
) -> tuple[bool, str]:
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            status, body = opener(check.url, 10.0)
        except Exception as exc:  # network error, timeout, etc.
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            text = body.decode("utf-8", errors="replace")
            status_ok = status == check.expect_status
            text_ok = check.expect_text is None or check.expect_text in text
            if status_ok and text_ok:
                return True, f"GET {check.url} -> {status} (ok, attempt {attempt}/{retries})"
            last_error = (
                f"GET {check.url} -> {status} (expected {check.expect_status}); "
                f"expect_text={check.expect_text!r} present={check.expect_text in text if check.expect_text else 'n/a'}"
            )
        if attempt < retries:
            sleep(delay_s)
    return False, f"smoke check failed after {retries} attempts: {last_error}"


def _default_opener(url: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "DarkFac-Smoke/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _win_kwargs_for_gh() -> dict[str, Any]:
    return _win_kwargs()


def _fetch_ticket_smoke_entries(pr_url: str, cwd: Path, *, gh_executable: str = "gh") -> list[str]:
    """Every ticket's `smoke` entry, parsed back out of the merged PR's body.

    Best-effort: any `gh` failure (not authenticated, PR already deleted,
    no repo context at `cwd`, ...) returns `[]` rather than raising -- ticket
    smoke checks are additive to `project.smoke`, never a hard requirement.
    """
    match = _PR_NUMBER_RE.search(pr_url or "")
    if not match:
        return []
    try:
        proc = subprocess.run(
            [gh_executable, "pr", "view", match.group(1), "--json", "body"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_win_kwargs_for_gh(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("gh pr view failed while fetching ticket smoke entries: %s", exc)
        return []
    if proc.returncode != 0:
        return []
    try:
        body = json.loads(proc.stdout or "{}").get("body", "")
    except json.JSONDecodeError:
        return []
    comment_match = _TICKET_SMOKE_COMMENT_RE.search(body or "")
    if not comment_match:
        return []
    try:
        data = json.loads(comment_match.group("json"))
    except json.JSONDecodeError:
        return []
    entries: list[str] = []
    if isinstance(data, dict):
        for values in data.values():
            if isinstance(values, list):
                entries.extend(str(v) for v in values)
    return entries


def _smoke_base_url(project: ProjectDescriptor, target_config: Optional["TargetConfig"]) -> Optional[str]:
    if project.domain:
        return f"https://{project.domain}"
    if target_config is not None and target_config.deploy_url:
        return target_config.deploy_url
    if target_config is not None and target_config.api_url:
        return target_config.api_url
    return None


class ReleaseStageHandler:
    """StageHandler for the deploy/smoke/rollback release stage (HF-27-07)."""

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://line/RunWorkspaceContext",
        output_schema_ref="schema://line/ReleaseOutcome",
        role="releaser",
        required_capabilities=["deploy"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        project: ProjectDescriptor,
        *,
        dokploy_adapter: Optional[DeploymentAdapter] = None,
        local_service_adapter: Optional[DeploymentAdapter] = None,
        ftp_adapter: Optional[DeploymentAdapter] = None,
        state_store: "Optional[ReleaseStateStore | GitRefReleaseStateStore]" = None,
        smoke_opener: Callable[[str, float], tuple[int, bytes]] = _default_opener,
        smoke_retries: int = _SMOKE_RETRIES,
        smoke_delay_s: float = _SMOKE_DELAY_S,
        reconcile_polls: int = _RECONCILE_POLLS,
        reconcile_delay_s: float = _RECONCILE_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
        validate_timeout_s: int = 1800,
    ) -> None:
        self.project = project
        self.dokploy_adapter = dokploy_adapter or DokployDeploymentAdapter()
        self.local_service_adapter = local_service_adapter or LocalServiceDeploymentAdapter()
        self.ftp_adapter = ftp_adapter or FtpMirrorDeploymentAdapter()
        self.state_store = state_store or default_state_store(project)
        self.smoke_opener = smoke_opener
        self.smoke_retries = smoke_retries
        self.smoke_delay_s = smoke_delay_s
        self.reconcile_polls = reconcile_polls
        self.reconcile_delay_s = reconcile_delay_s
        self.sleep = sleep
        self.validate_timeout_s = validate_timeout_s

    # ----------------------------------------------------------------
    # target/adapter selection
    # ----------------------------------------------------------------

    def _adapter_for(self, deploy_type: DeployTargetType) -> Optional[DeploymentAdapter]:
        return {
            DeployTargetType.DOKPLOY: self.dokploy_adapter,
            DeployTargetType.HOSTINGER_FTP: self.ftp_adapter,
            DeployTargetType.LOCAL_SERVICE: self.local_service_adapter,
        }.get(deploy_type)

    def _target_config(self, sha: str, last_good_sha: Optional[str]) -> TargetConfig:
        deploy = self.project.deploy
        params = dict(deploy.params) if deploy else {}
        healthcheck = params.get("healthcheck_endpoint") or params.get("health_endpoint")
        return TargetConfig(
            project_id=self.project.id,
            target_type=(deploy.type.value if deploy else DeployTargetType.NONE.value),
            environment=params.get("environment", "production"),
            api_url=params.get("api_url"),
            deploy_url=params.get("deploy_url"),
            service_name=params.get("service_name", self.project.id),
            host=params.get("host"),
            port=int(params["port"]) if params.get("port", "").isdigit() else None,
            healthcheck_endpoint=healthcheck,
            last_known_good_digest=last_good_sha,
            metadata=params,
        )

    # ----------------------------------------------------------------
    # deploy dispatch
    # ----------------------------------------------------------------

    def _run_restart_cmd(self, restart_cmd: str, cwd: Path) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                restart_cmd,
                cwd=str(cwd),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.validate_timeout_s,
                **_win_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return False, f"$ {restart_cmd}\n(timed out after {self.validate_timeout_s}s)"
        log = f"$ {restart_cmd}\n{proc.stdout}\n{proc.stderr}"
        return proc.returncode == 0, log

    def _deploy(
        self, sha: str, last_good_sha: Optional[str]
    ) -> tuple[Optional[DeploymentOperation], Optional[TargetConfig], Optional[StageResult]]:
        deploy = self.project.deploy
        deploy_type = deploy.type if deploy else DeployTargetType.NONE

        if deploy_type == DeployTargetType.NONE:
            return None, None, None

        target_config = self._target_config(sha, last_good_sha)
        adapter = self._adapter_for(deploy_type)
        if adapter is None:
            return None, None, StageResult(outcome="failed", cause_code=f"unknown_deploy_type:{deploy_type}")

        if deploy_type == DeployTargetType.LOCAL_SERVICE:
            restart_cmd = target_config.metadata.get("restart_cmd")
            if restart_cmd:
                ok, log = self._run_restart_cmd(restart_cmd, Path(self.project.path or "."))
                if not ok:
                    return None, target_config, StageResult(
                        outcome="retry", cause_code=f"restart_cmd_failed:\n{_truncate(log, 4000)}"
                    )

        artifact_ref = ArtifactRef(
            artifact_id=sha,
            source_sha=sha,
            byte_digest=sha,
            byte_size=0,
        )
        operation = adapter.start(artifact_ref, target_config)
        status = operation.status
        for _ in range(self.reconcile_polls):
            if status != DeploymentStatus.IN_PROGRESS:
                break
            self.sleep(self.reconcile_delay_s)
            status = adapter.reconcile(operation.operation_id)

        if status == DeploymentStatus.FAILED:
            return operation, target_config, StageResult(
                outcome="retry", cause_code=f"deploy_failed:{operation.operation_id}"
            )
        if status != DeploymentStatus.SUCCEEDED:
            return operation, target_config, StageResult(
                outcome="retry", cause_code=f"deploy_timed_out:{operation.operation_id}"
            )

        active_digest = adapter.installed_digest(target_config)
        if active_digest is not None and active_digest != sha:
            return operation, target_config, StageResult(
                outcome="retry",
                cause_code=f"installed_digest_mismatch:active={active_digest},expected={sha}",
            )

        return operation, target_config, None

    # ----------------------------------------------------------------
    # smoke
    # ----------------------------------------------------------------

    def _run_extra_smoke_entries(
        self, entries: list[str], target_config: Optional[TargetConfig]
    ) -> tuple[bool, str]:
        """HF-27-08 item G: run each ticket's `smoke` entry after `project.smoke`.

        An entry that parses as an absolute URL, or a `/`-rooted path (joined
        onto the deploy's base URL), becomes an HTTP `SmokeCheck`; anything
        else runs as a shell command, the same convention `commands.smoke`
        already uses. Same retry/rollback semantics as every other smoke
        check: any failure here fails the whole `_smoke()` call.
        """
        if not entries:
            return True, ""
        base_url = _smoke_base_url(self.project, target_config)
        logs: list[str] = []
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry:
                continue
            if entry.startswith("http://") or entry.startswith("https://"):
                url = entry
            elif entry.startswith("/") and base_url:
                url = base_url.rstrip("/") + entry
            else:
                try:
                    proc = subprocess.run(
                        entry,
                        cwd=self.project.path or ".",
                        shell=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.validate_timeout_s,
                        **_win_kwargs(),
                    )
                except subprocess.TimeoutExpired:
                    logs.append(f"$ {entry}\n(timed out)")
                    return False, _truncate("\n".join(logs), 8000)
                logs.append(f"$ {entry}\n{proc.stdout}\n{proc.stderr}")
                if proc.returncode != 0:
                    return False, _truncate("\n".join(logs), 8000)
                continue
            ok, log = _run_smoke_http(
                SmokeCheck(url=url),
                retries=self.smoke_retries,
                delay_s=self.smoke_delay_s,
                sleep=self.sleep,
                opener=self.smoke_opener,
            )
            logs.append(log)
            if not ok:
                return False, _truncate("\n".join(logs), 8000)
        return True, _truncate("\n".join(logs), 4000)

    def _smoke(
        self,
        sha: str,
        target_config: Optional[TargetConfig],
        adapter: Optional[DeploymentAdapter],
        extra_entries: list[str] = (),
    ) -> tuple[bool, str]:
        deploy = self.project.deploy
        if deploy is None or deploy.type == DeployTargetType.NONE:
            commands = resolve_commands(self.project, Path(self.project.path or "."))
            logs: list[str] = []
            if not commands.smoke and not extra_entries:
                return True, "(sem smoke local configurado; deploy 'none' considerado ok)"
            for cmd in commands.smoke:
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=self.project.path or ".",
                        shell=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.validate_timeout_s,
                        **_win_kwargs(),
                    )
                except subprocess.TimeoutExpired:
                    logs.append(f"$ {cmd}\n(timed out)")
                    return False, _truncate("\n".join(logs), 8000)
                logs.append(f"$ {cmd}\n{proc.stdout}\n{proc.stderr}")
                if proc.returncode != 0:
                    return False, _truncate("\n".join(logs), 8000)
            extra_ok, extra_log = self._run_extra_smoke_entries(list(extra_entries), target_config)
            if extra_log:
                logs.append(extra_log)
            if not extra_ok:
                return False, _truncate("\n".join(logs), 8000)
            return True, _truncate("\n".join(logs), 4000)

        if not self.project.smoke and not extra_entries:
            return True, "(sem SmokeCheck configurado para o projeto)"

        logs = []
        for check in self.project.smoke:
            ok, log = _run_smoke_http(
                check,
                retries=self.smoke_retries,
                delay_s=self.smoke_delay_s,
                sleep=self.sleep,
                opener=self.smoke_opener,
            )
            logs.append(log)
            if not ok:
                return False, _truncate("\n".join(logs), 8000)

        extra_ok, extra_log = self._run_extra_smoke_entries(list(extra_entries), target_config)
        if extra_log:
            logs.append(extra_log)
        if not extra_ok:
            return False, _truncate("\n".join(logs), 8000)

        if target_config is not None and adapter is not None and target_config.healthcheck_endpoint:
            active_digest = adapter.installed_digest(target_config)
            if active_digest is not None and active_digest != sha:
                logs.append(f"healthcheck sha mismatch: active={active_digest} expected={sha}")
                return False, _truncate("\n".join(logs), 8000)

        return True, _truncate("\n".join(logs), 4000)

    # ----------------------------------------------------------------
    # commercial acceptance
    # ----------------------------------------------------------------

    def _commercial_acceptance_guide(self, pr_url: str, merge_sha: str) -> StageResult:
        preview = ""
        if self.project.domain:
            preview = f"\nPreview/producao: https://{self.project.domain}"
        guide = (
            "kind=commercial_acceptance: este projeto exige aceite comercial antes do "
            "deploy automatico.\n"
            f"1. Revise o PR mergeado: {pr_url or '(url do PR indisponivel)'}\n"
            f"2. SHA a ser publicado: {merge_sha}"
            f"{preview}\n"
            "3. Quando aprovado pelo owner do negocio, confirme a aceitacao para que a "
            "fabrica prossiga com o deploy deste SHA."
        )
        return StageResult(outcome="waiting_human", cause_code=guide)

    # ----------------------------------------------------------------
    # entry point
    # ----------------------------------------------------------------

    def handle(self, context: StageContext) -> StageResult:
        run_id = context.claim.job_key.run_id
        refs = context.input_refs
        if not refs or not refs[-1]:
            return StageResult(outcome="failed", cause_code="release_missing_merge_sha")
        merge_sha = refs[-1]
        pr_url = refs[0] if len(refs) > 1 else ""

        state = self.state_store.load(self.project.id)
        run_state = state.runs.get(run_id, ReleaseRunState())

        # Idempotency: this exact SHA was already deployed and passed smoke.
        if state.last_good_sha == merge_sha:
            return StageResult(
                outcome="success",
                output_refs=[
                    state.last_success_operation_id or f"release:{self.project.id}:{merge_sha[:12]}",
                    state.last_success_smoke_ref or f"smoke:{run_id}:{merge_sha[:12]}",
                ],
            )

        # Only an acceptance recorded by the owner (record_commercial_acceptance)
        # opens the gate; re-invoking the stage must never open it by itself.
        if self.project.requires_commercial_acceptance and run_state.commercial_accepted_sha != merge_sha:
            return self._commercial_acceptance_guide(pr_url, merge_sha)

        operation, target_config, deploy_error = self._deploy(merge_sha, state.last_good_sha)
        if deploy_error is not None:
            return deploy_error

        deploy_type = self.project.deploy.type if self.project.deploy else DeployTargetType.NONE
        adapter = self._adapter_for(deploy_type)
        ticket_smoke_entries = _fetch_ticket_smoke_entries(pr_url, Path(self.project.path or "."))
        smoke_ok, smoke_log = self._smoke(merge_sha, target_config, adapter, extra_entries=ticket_smoke_entries)

        if smoke_ok:
            state.last_good_sha = merge_sha
            state.last_success_operation_id = operation.operation_id if operation else f"local:{merge_sha}"
            smoke_ref = f"smoke:{run_id}:{merge_sha[:12]}"
            state.last_success_smoke_ref = smoke_ref
            run_state.consecutive_smoke_failures = 0
            run_state.last_sha = merge_sha
            state.runs[run_id] = run_state
            self.state_store.save(self.project.id, state)
            return StageResult(outcome="success", output_refs=[state.last_success_operation_id, smoke_ref])

        # Smoke failed.
        run_state.consecutive_smoke_failures += 1
        run_state.last_sha = merge_sha
        state.runs[run_id] = run_state

        if run_state.consecutive_smoke_failures >= _MAX_CONSECUTIVE_SMOKE_FAILURES:
            self.state_store.save(self.project.id, state)
            logger.error(
                "release %s: smoke failed twice in a row for sha %s; giving up (prod_smoke_failed)",
                run_id, merge_sha,
            )
            return StageResult(
                outcome="failed",
                cause_code=f"prod_smoke_failed:\n{smoke_log}",
            )

        if adapter is None or target_config is None or not state.last_good_sha:
            # No deploy target (local-only smoke) or nothing to roll back to.
            self.state_store.save(self.project.id, state)
            return StageResult(outcome="retry", cause_code=f"smoke_failed_no_rollback_target:\n{smoke_log}")

        target_config.last_known_good_digest = state.last_good_sha
        self.state_store.save(self.project.id, state)
        rollback = adapter.rollback(
            target_config=target_config,
            failed_digest=merge_sha,
            reason="post-deploy smoke check failed",
            claim=context.claim,
        )
        # Production must be proven back up: a failed rollback, or a previous
        # SHA that no longer passes smoke, needs a human, not another retry.
        if rollback.status == DeploymentStatus.FAILED or rollback.restored_digest != state.last_good_sha:
            return StageResult(
                outcome="failed",
                cause_code=(
                    f"rollback_failed:status={rollback.status.value},"
                    f"restored={rollback.restored_digest}\n{smoke_log}"
                ),
            )
        restored_ok, restored_log = self._smoke(state.last_good_sha, target_config, adapter)
        if not restored_ok:
            return StageResult(
                outcome="failed",
                cause_code=f"rollback_smoke_failed:{state.last_good_sha[:12]}\n{restored_log}\n{smoke_log}",
            )
        return StageResult(
            outcome="retry",
            cause_code=f"prod_smoke_failed_rolled_back_to_{state.last_good_sha[:12]}:\n{smoke_log}",
        )


__all__ = [
    "ReleaseStageHandler",
    "ReleaseError",
    "ReleaseState",
    "ReleaseRunState",
    "ReleaseStateStore",
]
