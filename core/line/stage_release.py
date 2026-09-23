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
   (when configured) a preview URL, before touching the deploy target.
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
6. **State** — `last_good_sha` (and the small bit of per-run failure-streak
   bookkeeping needed for step 5) is persisted to a small JSON file under
   `core.line.workspace.workspace_root()/<project_id>/release_state.json` —
   project-level state that must outlive the per-run `df/<run_id>` branch
   (which HF-27-06 deletes on merge), so it cannot live on that branch like
   the rest of the line's per-run context.
"""

from __future__ import annotations

import json
import logging
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
from core.projects.registry import resolve_commands
from core.workflow.control_contracts import HandlerDescriptor, StageContext, StageResult

logger = logging.getLogger(__name__)

STAGE = "release"
VERSION = "v1"

_SMOKE_RETRIES = 5
_SMOKE_DELAY_S = 10.0
_RECONCILE_POLLS = 30
_RECONCILE_DELAY_S = 1.0
_MAX_CONSECUTIVE_SMOKE_FAILURES = 2


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
    """Loads/saves `ReleaseState` as JSON, one file per project."""

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
        state_store: Optional[ReleaseStateStore] = None,
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
        self.state_store = state_store or ReleaseStateStore()
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

    def _smoke(self, sha: str, target_config: Optional[TargetConfig], adapter: Optional[DeploymentAdapter]) -> tuple[bool, str]:
        deploy = self.project.deploy
        if deploy is None or deploy.type == DeployTargetType.NONE:
            commands = resolve_commands(self.project, Path(self.project.path or "."))
            if not commands.smoke:
                return True, "(sem smoke local configurado; deploy 'none' considerado ok)"
            logs: list[str] = []
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
            return True, _truncate("\n".join(logs), 4000)

        if not self.project.smoke:
            return True, "(sem SmokeCheck configurado para o projeto)"

        logs: list[str] = []
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

        # Idempotency: this exact SHA was already deployed successfully.
        if state.last_good_sha == merge_sha and state.last_success_operation_id:
            return StageResult(
                outcome="success",
                output_refs=[state.last_success_operation_id, state.last_success_smoke_ref or ""],
            )

        if self.project.requires_commercial_acceptance and run_state.commercial_accepted_sha != merge_sha:
            run_state.commercial_accepted_sha = merge_sha
            state.runs[run_id] = run_state
            self.state_store.save(self.project.id, state)
            return self._commercial_acceptance_guide(pr_url, merge_sha)

        operation, target_config, deploy_error = self._deploy(merge_sha, state.last_good_sha)
        if deploy_error is not None:
            return deploy_error

        deploy_type = self.project.deploy.type if self.project.deploy else DeployTargetType.NONE
        adapter = self._adapter_for(deploy_type)
        smoke_ok, smoke_log = self._smoke(merge_sha, target_config, adapter)

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
        adapter.rollback(
            target_config=target_config,
            failed_digest=merge_sha,
            reason="post-deploy smoke check failed",
            claim=context.claim,
        )
        self.state_store.save(self.project.id, state)
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
