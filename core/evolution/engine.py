"""Factory Self-Evolution Engine for DarkFac (HF-25).

Orchestrates the lifecycle of evolutionary proposals: creation, isolated holdout
evaluation, safe promotion with atomic rollback snapshots, and skill mirroring.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import project_root
from core.evolution.models import (
    EvolutionProposal,
    EvolutionReport,
    EvolutionStatus,
    EvolutionTarget,
    EvolutionTrigger,
    HoldoutEvaluationResult,
    RollbackSnapshot,
    SecurityViolationError,
)
from core.evolution.sandbox import EvolutionHoldoutSandbox

logger = logging.getLogger(__name__)

DEFAULT_EVOLUTION_DIR = Path(".factory") / "evolution"
DEFAULT_PROPOSALS_FILE = DEFAULT_EVOLUTION_DIR / "proposals.json"
DEFAULT_ROLLBACKS_DIR = DEFAULT_EVOLUTION_DIR / "rollbacks"


class FactoryEvolutionEngine:
    """Governs safe self-evolution, verification gates, promotion and rollbacks."""

    def __init__(
        self,
        root: Path | None = None,
        storage_dir: Path | None = None,
    ) -> None:
        self.root = Path(root) if root else project_root()
        self.storage_dir = Path(storage_dir) if storage_dir else (self.root / DEFAULT_EVOLUTION_DIR)
        self.proposals_file = self.storage_dir / "proposals.json"
        self.rollbacks_dir = self.storage_dir / "rollbacks"
        self.sandbox = EvolutionHoldoutSandbox(self.root)
        self._proposals: dict[str, EvolutionProposal] = {}
        self._snapshots: dict[str, RollbackSnapshot] = {}
        self._running_jobs: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        """Load persisted proposals and rollback records."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.rollbacks_dir.mkdir(parents=True, exist_ok=True)

        if self.proposals_file.is_file():
            try:
                data = json.loads(self.proposals_file.read_text(encoding="utf-8"))
                for item in data:
                    prop = EvolutionProposal.model_validate(item)
                    self._proposals[prop.proposal_id] = prop
            except Exception as exc:
                logger.error(f"Failed to load evolution proposals from {self.proposals_file}: {exc}")

        # Scan rollback snapshots
        for snap_file in self.rollbacks_dir.glob("snap_*.json"):
            try:
                data = json.loads(snap_file.read_text(encoding="utf-8"))
                snap = RollbackSnapshot.model_validate(data)
                self._snapshots[snap.proposal_id] = snap
            except Exception as exc:
                logger.error(f"Failed to load rollback snapshot {snap_file}: {exc}")

    def _save(self) -> None:
        """Persist proposals to disk."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        payload = [p.model_dump(mode="json") for p in self._proposals.values()]
        self.proposals_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def propose(
        self,
        target_kind: EvolutionTarget | str,
        target_path: str,
        trigger: EvolutionTrigger | str,
        patch_content: str,
        rationale: str,
        *,
        proposal_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvolutionProposal:
        """Register a new proposed mutation, auditing boundaries immediately."""
        norm_kind = EvolutionTarget(target_kind) if isinstance(target_kind, str) else target_kind
        norm_trigger = EvolutionTrigger(trigger) if isinstance(trigger, str) else trigger
        norm_path = target_path.replace("\\", "/").strip().lstrip("/")

        # Fail-closed boundary check
        self.sandbox.audit_boundaries(norm_path)

        # Check against blocked / rolled back proposals to prevent spurious re-proposals
        for existing in self._proposals.values():
            if (
                existing.target_path == norm_path
                and existing.status == EvolutionStatus.ROLLED_BACK
                and existing.metadata.get("blocked", False)
                and existing.patch_content == patch_content
            ):
                raise ValueError(
                    f"Spurious evolution proposal rejected: '{norm_path}' was previously rolled back "
                    f"for regression (proposal: {existing.proposal_id}) and is blocked."
                )

        pid = proposal_id or f"evo_{uuid.uuid4().hex[:8]}"
        proposal = EvolutionProposal(
            proposal_id=pid,
            target_kind=norm_kind,
            target_path=norm_path,
            trigger=norm_trigger,
            patch_content=patch_content,
            rationale=rationale.strip(),
            status=EvolutionStatus.PROPOSED,
            metadata=dict(metadata or {}),
        )
        self._proposals[pid] = proposal
        self._save()
        return proposal

    def evaluate_candidate(
        self,
        proposal_id: str,
        *,
        holdout_cmd: str | None = None,
        timeout_sec: int = 60,
    ) -> HoldoutEvaluationResult:
        """Evaluate a proposal in the isolated holdout sandbox."""
        if proposal_id not in self._proposals:
            raise KeyError(f"Unknown evolution proposal: {proposal_id}")

        prop = self._proposals[proposal_id]
        prop.status = EvolutionStatus.EVALUATING
        self._save()

        result = self.sandbox.evaluate(prop, holdout_cmd=holdout_cmd, timeout_sec=timeout_sec)
        if result.passed:
            prop.status = EvolutionStatus.APPROVED
        else:
            prop.status = EvolutionStatus.REJECTED

        self._save()
        return result

    def promote_candidate(
        self,
        proposal_id: str,
        *,
        defer_to_restart: bool = False,
    ) -> RollbackSnapshot:
        """Safely promote an approved candidate, capturing an atomic rollback snapshot."""
        if proposal_id not in self._proposals:
            raise KeyError(f"Unknown evolution proposal: {proposal_id}")

        prop = self._proposals[proposal_id]
        if prop.status == EvolutionStatus.ROLLED_BACK or prop.metadata.get("blocked", False):
            raise ValueError(
                f"Cannot promote proposal '{proposal_id}' with status '{prop.status}'. "
                "It was rolled back and is blocked against spurious re-application."
            )
        if prop.status != EvolutionStatus.APPROVED:
            raise ValueError(f"Cannot promote proposal with status '{prop.status}'. Must be 'approved'.")

        self.sandbox.audit_boundaries(prop.target_path)
        target_abs = self.root / prop.target_path

        # Capture snapshot of previous content
        previous_content = target_abs.read_text(encoding="utf-8") if target_abs.is_file() else ""
        prev_hash = hashlib.sha256(previous_content.encode("utf-8")).hexdigest()

        snapshot_id = f"snap_{uuid.uuid4().hex[:8]}"
        snapshot = RollbackSnapshot(
            snapshot_id=snapshot_id,
            proposal_id=proposal_id,
            target_path=prop.target_path,
            previous_content=previous_content,
            previous_hash=prev_hash,
        )

        # Persist snapshot
        snap_file = self.rollbacks_dir / f"{snapshot_id}.json"
        snap_file.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        self._snapshots[proposal_id] = snapshot

        # If deferred to restart, do NOT mutate the active file immediately
        if defer_to_restart:
            prop.status = EvolutionStatus.PROMOTED
            prop.promoted_at = datetime.now(timezone.utc)
            prop.metadata["pending_restart"] = True
            prop.metadata["applied_at_restart"] = None
            self._save()
            return snapshot

        # Apply mutation immediately
        target_abs.parent.mkdir(parents=True, exist_ok=True)
        target_abs.write_text(prop.patch_content, encoding="utf-8")

        # Update status
        prop.status = EvolutionStatus.PROMOTED
        prop.promoted_at = datetime.now(timezone.utc)
        prop.metadata["pending_restart"] = False
        prop.metadata["applied_at_restart"] = datetime.now(timezone.utc).isoformat()
        self._save()

        # If skill modified, trigger skill sync
        if prop.target_path.startswith(".agents/skills/"):
            self._sync_skills()

        return snapshot

    def apply_pending_promotions(self) -> list[str]:
        """Apply all approved promotions that were deferred to restart."""
        applied = []
        for pid, prop in list(self._proposals.items()):
            if prop.status == EvolutionStatus.PROMOTED and prop.metadata.get("pending_restart", False):
                if prop.metadata.get("blocked", False) or prop.status == EvolutionStatus.ROLLED_BACK:
                    continue
                # Fail-closed audit before writing
                self.sandbox.audit_boundaries(prop.target_path)
                target_abs = self.root / prop.target_path
                target_abs.parent.mkdir(parents=True, exist_ok=True)
                target_abs.write_text(prop.patch_content, encoding="utf-8")
                prop.metadata["pending_restart"] = False
                prop.metadata["applied_at_restart"] = datetime.now(timezone.utc).isoformat()
                applied.append(pid)
                if prop.target_path.startswith(".agents/skills/"):
                    self._sync_skills()
        if applied:
            self._save()
        return applied

    def restart(self) -> list[str]:
        """Simulate system/worker restart: clear running jobs and apply pending promotions."""
        self._running_jobs.clear()
        applied = self.apply_pending_promotions()
        self._load()
        return applied

    def register_running_job(self, run_id: str, paths: list[str] | None = None) -> None:
        """Register an active job run, pinning its observed file contents."""
        pinned = {}
        for p in (paths or []):
            norm_p = p.replace("\\", "/").strip().lstrip("/")
            abs_p = self.root / norm_p
            if abs_p.is_file():
                pinned[norm_p] = abs_p.read_text(encoding="utf-8")
        self._running_jobs[run_id] = pinned

    def get_content_for_run(self, run_id: str, target_path: str) -> str:
        """Retrieve target file content for a run, isolating running jobs from mid-flight mutations."""
        norm_path = target_path.replace("\\", "/").strip().lstrip("/")
        if run_id in self._running_jobs and norm_path in self._running_jobs[run_id]:
            return self._running_jobs[run_id][norm_path]
        abs_p = self.root / norm_path
        return abs_p.read_text(encoding="utf-8") if abs_p.is_file() else ""

    def finish_running_job(self, run_id: str) -> None:
        """Unregister a finished running job."""
        self._running_jobs.pop(run_id, None)

    def rollback_candidate(self, proposal_id: str, reason: str = "") -> bool:
        """Rollback an active or promoted mutation using its captured snapshot."""
        if proposal_id not in self._snapshots:
            raise KeyError(f"No rollback snapshot found for proposal: {proposal_id}")

        snapshot = self._snapshots[proposal_id]
        target_abs = self.root / snapshot.target_path

        if snapshot.previous_content:
            target_abs.parent.mkdir(parents=True, exist_ok=True)
            target_abs.write_text(snapshot.previous_content, encoding="utf-8")
        elif target_abs.is_file():
            target_abs.unlink()

        if proposal_id in self._proposals:
            prop = self._proposals[proposal_id]
            prop.status = EvolutionStatus.ROLLED_BACK
            prop.retired_at = datetime.now(timezone.utc)
            prop.metadata["blocked"] = True
            prop.metadata["pending_restart"] = False
            if reason:
                prop.metadata["rollback_reason"] = reason
            self._save()

        if snapshot.target_path.startswith(".agents/skills/"):
            self._sync_skills()

        return True

    def record_regression(self, proposal_id: str, reason: str = "") -> RollbackSnapshot:
        """Operational regression detected: rollback exact snapshot and block re-application."""
        if proposal_id not in self._snapshots:
            raise KeyError(f"No rollback snapshot found for proposal: {proposal_id}")

        self.rollback_candidate(proposal_id, reason=reason or "Operational regression detected")
        return self._snapshots[proposal_id]

    def _sync_skills(self) -> None:
        """Run scripts/sync_skills.py to keep .claude/skills synchronized with .agents/skills."""
        sync_script = self.root / "scripts" / "sync_skills.py"
        if sync_script.is_file():
            try:
                subprocess.run(
                    [sys.executable, str(sync_script)],
                    cwd=str(self.root),
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            except Exception as exc:
                logger.warning(f"Auto-sync of skills encountered error: {exc}")

    def list_proposals(self, status: EvolutionStatus | None = None) -> list[EvolutionProposal]:
        """Return all proposals, optionally filtered by lifecycle status."""
        items = list(self._proposals.values())
        if status:
            items = [p for p in items if p.status == status]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def get_report(self) -> EvolutionReport:
        """Generate a complete diagnostic report of the evolution subsystem."""
        proposals = self.list_proposals()
        active = sum(1 for p in proposals if p.status == EvolutionStatus.PROMOTED)
        rejected = sum(1 for p in proposals if p.status == EvolutionStatus.REJECTED)
        return EvolutionReport(
            total_proposals=len(proposals),
            active_promotions=active,
            rejected_count=rejected,
            proposals=proposals,
            snapshots=list(self._snapshots.values()),
        )
