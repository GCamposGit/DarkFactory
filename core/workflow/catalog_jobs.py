"""Catalog refresh stage handler and run-level model pinning (HF-07-03).

Governed by Universal Engineering Standards (AGENTS.md), HF-07-03, and CONTRACTS.md.
Implements:
1. Stage ("catalog_refresh", "v1") handler.
2. Persisted 24h timer with boot catchup.
3. Idempotent refresh execution within 24h window.
4. Run-level model pinning: promotion ONLY to new jobs, active jobs retain pinned version.
5. Rollback/Fallback: failure preserves last valid catalog with age and warning metadata.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.benchmarks.qualification import (
    ModelCandidate,
    ModelQualificationRecord,
    QualificationEngine,
)
from core.workflow.control_contracts import (
    HandlerDescriptor,
    StageContext,
    StageResult,
)
from core.workflow.qualified_routes import _EMBEDDED_DEFAULT_CATALOG

logger = logging.getLogger("darkfac.workflow.catalog_jobs")

DEFAULT_STATE_PATH = Path(".factory/planning/continuous-autonomy/state/catalog_refresh_state.json")
DEFAULT_CATALOG_PATH = Path(".factory/planning/continuous-autonomy/bindings/executors.json")


class CatalogRefreshState(BaseModel):
    """Persisted timer and execution state for catalog refresh."""

    model_config = ConfigDict(extra="ignore")

    last_refresh_timestamp: Optional[str] = None
    catalog_version: str = "initial"
    last_status: str = "uninitialized"  # "success", "fallback_retained", "failed"
    age_hours_at_last_attempt: float = 0.0
    warning: Optional[str] = None
    refresh_count: int = 0
    last_attempt_timestamp: Optional[str] = None
    degraded_at: Optional[str] = None
    pinned_run_ids: List[str] = Field(default_factory=list)
    pinned_catalogs: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    pinned_versions: Dict[str, str] = Field(default_factory=dict)


class CatalogRefreshHandler:
    """StageHandler for catalog_refresh (v1).

    Maintains persisted 24h timer, boot catchup, idempotency, run-level pinning,
    and fallback preservation.
    """

    STAGE: str = "catalog_refresh"
    VERSION: str = "v1"

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage="catalog_refresh",
        version="v1",
        input_schema_ref="schemas/catalog_refresh_input.json",
        output_schema_ref="schemas/catalog_refresh_output.json",
        role="economy",
        required_capabilities=["catalog_refresh"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        state_path: Path = DEFAULT_STATE_PATH,
        catalog_path: Path = DEFAULT_CATALOG_PATH,
        engine: Optional[QualificationEngine] = None,
        candidate_provider: Optional[Callable[[], List[ModelCandidate]]] = None,
        base_catalog: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.catalog_path = Path(catalog_path)
        self.engine = engine or QualificationEngine()
        self.candidate_provider = candidate_provider
        self.base_catalog = deepcopy(base_catalog if base_catalog is not None else _EMBEDDED_DEFAULT_CATALOG)

        # Active catalog and version promoted to new jobs
        self.active_catalog: Dict[str, Any] = {}
        self.active_version: str = "v1"

        # Run-level model pinning storage
        self._pinned_catalogs_by_run: Dict[str, Dict[str, Any]] = {}
        self._pinned_versions_by_run: Dict[str, str] = {}

        # Last valid catalog backup for fallback
        self._last_valid_catalog: Optional[Dict[str, Any]] = None
        self._last_valid_timestamp: Optional[datetime] = None

        self.state = CatalogRefreshState()
        self._load_state_and_catalog()

    def _load_state_and_catalog(self) -> None:
        """Load persisted state and catalog from disk if available."""
        if self.catalog_path.is_file():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                if self._validate_catalog(data):
                    self.active_catalog = deepcopy(data)
                    self._last_valid_catalog = deepcopy(data)
            except Exception as exc:
                logger.warning("Failed to load catalog from %s: %s", self.catalog_path, exc)

        if not self.active_catalog:
            self.active_catalog = json.loads(json.dumps(self.base_catalog))
            self._last_valid_catalog = self.active_catalog

        if self.state_path.is_file():
            try:
                state_data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.state = CatalogRefreshState.model_validate(state_data)
                self.active_version = self.state.catalog_version
                self._pinned_catalogs_by_run = {
                    run_id: deepcopy(catalog)
                    for run_id, catalog in self.state.pinned_catalogs.items()
                    if isinstance(run_id, str)
                    and run_id
                    and self._validate_catalog(catalog)
                }
                self._pinned_versions_by_run = {
                    run_id: version
                    for run_id, version in self.state.pinned_versions.items()
                    if run_id in self._pinned_catalogs_by_run and version
                }
                if self.state.last_refresh_timestamp:
                    self._last_valid_timestamp = self._as_utc(
                        datetime.fromisoformat(self.state.last_refresh_timestamp)
                    )
            except Exception as exc:
                logger.warning("Failed to load state from %s: %s", self.state_path, exc)

    def _save_state_and_catalog(self) -> None:
        """Persist current state and active catalog to disk."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)

        self.state.catalog_version = self.active_version
        self.state.pinned_run_ids = list(self._pinned_catalogs_by_run.keys())
        self.state.pinned_catalogs = deepcopy(self._pinned_catalogs_by_run)
        self.state.pinned_versions = dict(self._pinned_versions_by_run)

        self._atomic_write_json(self.catalog_path, self.active_catalog)
        self._atomic_write_json(self.state_path, self.state.model_dump())

    @staticmethod
    def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
        """Write a JSON artifact through a same-directory temporary file."""
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _validate_catalog(self, catalog: Dict[str, Any]) -> bool:
        """Verify catalog contains required providers and roles structure."""
        if not isinstance(catalog, dict):
            return False
        if "providers" not in catalog or "roles_mapping" not in catalog:
            return False
        providers = catalog["providers"]
        roles = catalog["roles_mapping"]
        if not isinstance(providers, dict) or not providers:
            return False
        if not isinstance(roles, dict):
            return False
        # Must define at least economy and high_architecture
        if "economy" not in roles or "high_architecture" not in roles:
            return False
        for provider in providers.values():
            if not isinstance(provider, dict) or not isinstance(provider.get("models"), list):
                return False
            if not provider.get("provider_id"):
                return False
        for role_name in ("economy", "high_architecture"):
            role = roles[role_name]
            if not isinstance(role, dict) or not isinstance(role.get("allowed_models"), list):
                return False
            if not all(isinstance(model_id, str) and model_id for model_id in role["allowed_models"]):
                return False
        return True

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize caller-provided timestamps for deterministic age checks."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def check_boot_catchup(self, now: Optional[datetime] = None) -> bool:
        """Check if 24h refresh is overdue or required on startup."""
        current_time = self._as_utc(now or datetime.now(UTC))
        if not self.state.last_refresh_timestamp:
            return True

        try:
            last_dt = self._as_utc(datetime.fromisoformat(self.state.last_refresh_timestamp))
        except Exception:
            return True

        return (current_time - last_dt) >= timedelta(hours=24)

    def is_refresh_due(self, now: Optional[datetime] = None) -> bool:
        """Check whether refresh is due (alias for 24h elapsed or uninitialized)."""
        return self.check_boot_catchup(now=now)

    def get_catalog_age_hours(self, now: Optional[datetime] = None) -> float:
        """Calculate age of current catalog in hours."""
        current_time = self._as_utc(now or datetime.now(UTC))
        ts = self._last_valid_timestamp
        if not ts and self.state.last_refresh_timestamp:
            try:
                ts = self._as_utc(datetime.fromisoformat(self.state.last_refresh_timestamp))
            except Exception:
                ts = None

        if not ts:
            return 0.0

        delta = current_time - ts
        return max(0.0, round(delta.total_seconds() / 3600.0, 2))

    def get_catalog_for_run(self, run_id: str) -> Dict[str, Any]:
        """Retrieve catalog for a run, enforcing run-level model pinning.

        Active jobs retain their pinned version; new jobs receive the active catalog.
        """
        if run_id in self._pinned_catalogs_by_run:
            return deepcopy(self._pinned_catalogs_by_run[run_id])

        # Pin this run to the current active catalog and version
        pinned = json.loads(json.dumps(self.active_catalog))
        self._pinned_catalogs_by_run[run_id] = pinned
        self._pinned_versions_by_run[run_id] = self.active_version
        self._save_state_and_catalog()
        return deepcopy(pinned)

    def get_version_for_run(self, run_id: str) -> str:
        """Return the pinned version string for a run."""
        if run_id in self._pinned_versions_by_run:
            return self._pinned_versions_by_run[run_id]
        # Trigger pinning
        self.get_catalog_for_run(run_id)
        return self._pinned_versions_by_run[run_id]

    def refresh_catalog(
        self,
        now: Optional[datetime] = None,
        force: bool = False,
        candidates: Optional[List[ModelCandidate]] = None,
    ) -> Dict[str, Any]:
        """Execute catalog refresh with idempotency, qualification, and fallback.

        - Idempotency: If within 24h and not forced, returns active catalog without re-running.
        - Promotion: Newly generated catalog is promoted to self.active_catalog (affects only new jobs).
        - Fallback: If qualification or validation fails, preserves last valid catalog with age and warning.
        """
        current_time = self._as_utc(now or datetime.now(UTC))

        # Idempotency check: 24h window
        if not force and not self.is_refresh_due(current_time):
            logger.info("Catalog refresh skipped: within 24h window (idempotent).")
            return self.active_catalog

        # Attempt refresh
        try:
            candidate_list = candidates
            if candidate_list is None:
                if self.candidate_provider:
                    candidate_list = self.candidate_provider()
                else:
                    candidate_list = []

            if not candidate_list:
                raise ValueError("No model candidates available for qualification.")

            # Qualify candidates and compute Pareto frontier
            records = self.engine.qualify_all(candidate_list)
            new_catalog = self.engine.generate_catalog(records, base_catalog=self.base_catalog)

            if not self._validate_catalog(new_catalog):
                raise ValueError("Generated catalog failed structural validation.")

            # Ensure at least one model is qualified for economy and high_architecture
            roles = new_catalog.get("roles_mapping", {})
            if not roles.get("economy", {}).get("allowed_models"):
                raise ValueError("No models qualified for economy role.")
            if not roles.get("high_architecture", {}).get("allowed_models"):
                raise ValueError("No models qualified for high_architecture role.")

            # Successful promotion!
            new_version = f"v{current_time.strftime('%Y%m%d%H%M%S')}"
            if new_version == self.active_version:
                new_version = f"{new_version}-r{self.state.refresh_count + 1}"
            self.active_catalog = new_catalog
            self.active_version = new_version
            self._last_valid_catalog = deepcopy(new_catalog)
            self._last_valid_timestamp = current_time

            self.state.last_refresh_timestamp = current_time.isoformat()
            self.state.last_attempt_timestamp = current_time.isoformat()
            self.state.last_status = "success"
            self.state.warning = None
            self.state.degraded_at = None
            self.state.age_hours_at_last_attempt = 0.0
            self.state.refresh_count += 1
            self._save_state_and_catalog()

            logger.info("Catalog promoted successfully to version %s.", new_version)
            return self.active_catalog

        except Exception as exc:
            # Fallback & Rollback: Maintain last valid catalog with age & warning
            age_hours = self.get_catalog_age_hours(current_time)
            warning_msg = (
                f"Catalog refresh failed: {exc!s}; retained last valid catalog "
                f"version '{self.active_version}' (age: {age_hours:.1f}h)."
            )
            logger.warning(warning_msg)

            if self._last_valid_catalog:
                self.active_catalog = self._last_valid_catalog

            self.state.last_status = "fallback_retained"
            self.state.warning = warning_msg
            self.state.age_hours_at_last_attempt = age_hours
            self.state.last_attempt_timestamp = current_time.isoformat()
            self.state.degraded_at = current_time.isoformat()
            self._save_state_and_catalog()

            return self.active_catalog

    def handle(self, context: StageContext) -> StageResult:
        """Handle execution of stage ('catalog_refresh', 'v1')."""
        current_time = datetime.now(UTC)

        # Execute refresh (boot catchup or scheduled trigger)
        catalog = self.refresh_catalog(now=current_time)

        # Output artifact paths
        output_refs = [str(self.catalog_path.resolve())]
        evidence_refs = [str(self.state_path.resolve())]

        return StageResult(
            outcome="success",
            output_refs=output_refs,
            evidence_refs=evidence_refs,
            actual_cost=0.0,
            cause_code=None,
        )
