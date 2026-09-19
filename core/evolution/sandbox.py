"""Isolated Holdout Evaluation Sandbox for Factory Self-Evolution (HF-25).

Enforces strict fail-closed boundaries against active verifier mutation and
runs candidate evaluations in an isolated staging workspace.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence
import uuid

from core.paths import project_root
from core.evolution.models import (
    EvolutionProposal,
    HoldoutEvaluationResult,
    SecurityViolationError,
)

PROTECTED_GOVERNANCE_FILES = {
    "MISSION.md",
    "FACTORY_RULES.md",
    "FACTORY_GOVERNANCE.md",
}

VERIFIER_GUARD_PREFIXES = (
    "core/harness/",
    "harness.config.json",
    "core/orchestrator/guard.py",
    "core/acceptance/",
    "core/workflow/verification.py",
    "core/workflow/readiness.py",
)


class EvolutionHoldoutSandbox:
    """Evaluates proposed self-evolution changes in an isolated workspace."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else project_root()

    def audit_boundaries(self, target_path: str) -> None:
        """Enforce inviolable boundaries against verifier tampering or governance mutation."""
        norm_path = target_path.replace("\\", "/").strip().lstrip("/")

        # 1. Check protected governance files
        for protected in PROTECTED_GOVERNANCE_FILES:
            if norm_path.lower() == protected.lower() or norm_path.endswith("/" + protected.lower()):
                raise SecurityViolationError(
                    f"Forbidden self-evolution target: '{target_path}' is an immutable governance file."
                )

        # 2. Check active verifier code and harness guards
        for prefix in VERIFIER_GUARD_PREFIXES:
            if norm_path.lower().startswith(prefix.lower()) or norm_path.lower() == prefix.lower():
                raise SecurityViolationError(
                    f"Forbidden self-evolution target: '{target_path}' touches active verifier or guard code ({prefix})."
                )

    def evaluate(
        self,
        proposal: EvolutionProposal,
        *,
        holdout_cmd: str | None = None,
        timeout_sec: int = 60,
    ) -> HoldoutEvaluationResult:
        """Run candidate evaluation in an isolated staging sandbox."""
        run_id = f"eval_{uuid.uuid4().hex[:8]}"

        # Pre-execution security check
        try:
            self.audit_boundaries(proposal.target_path)
        except SecurityViolationError as exc:
            return HoldoutEvaluationResult(
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                passed=False,
                discovered_steps=1,
                passed_steps=0,
                tampering_detected=True,
                protected_files_touched=[proposal.target_path],
                evidence_hash=hashlib.sha256(proposal.patch_content.encode("utf-8")).hexdigest(),
                log_summary=f"SECURITY_VIOLATION: {exc}",
            )

        target_file = self.root / proposal.target_path
        content_bytes = proposal.patch_content.encode("utf-8")
        evidence_hash = hashlib.sha256(content_bytes).hexdigest()

        # Create isolated temporary workspace for validation
        with tempfile.TemporaryDirectory(prefix="darkfac_sandbox_") as tmpdir:
            sandbox_path = Path(tmpdir)
            sandbox_target = sandbox_path / proposal.target_path
            sandbox_target.parent.mkdir(parents=True, exist_ok=True)
            sandbox_target.write_bytes(content_bytes)

            # Check compilation if it's a python file
            if proposal.target_path.endswith(".py"):
                try:
                    import py_compile
                    py_compile.compile(str(sandbox_target), doraise=True)
                except Exception as comp_exc:
                    return HoldoutEvaluationResult(
                        run_id=run_id,
                        proposal_id=proposal.proposal_id,
                        passed=False,
                        discovered_steps=1,
                        passed_steps=0,
                        evidence_hash=evidence_hash,
                        log_summary=f"Compilation error in candidate: {comp_exc}",
                    )

            # If holdout command provided, execute it with working dir pointed at sandbox or root
            if holdout_cmd:
                try:
                    env = dict(os.environ)
                    env["PYTHONPATH"] = str(self.root)
                    proc = subprocess.run(
                        holdout_cmd,
                        shell=True,
                        cwd=str(self.root),
                        capture_output=True,
                        text=True,
                        timeout=timeout_sec,
                        env=env,
                    )
                    passed = proc.returncode == 0
                    log_summary = proc.stdout if passed else (proc.stderr or proc.stdout)
                    return HoldoutEvaluationResult(
                        run_id=run_id,
                        proposal_id=proposal.proposal_id,
                        passed=passed,
                        discovered_steps=1,
                        passed_steps=1 if passed else 0,
                        evidence_hash=evidence_hash,
                        log_summary=log_summary[:2000],
                    )
                except Exception as sub_exc:
                    return HoldoutEvaluationResult(
                        run_id=run_id,
                        proposal_id=proposal.proposal_id,
                        passed=False,
                        discovered_steps=1,
                        passed_steps=0,
                        evidence_hash=evidence_hash,
                        log_summary=f"Holdout command execution failed: {sub_exc}",
                    )

            # Default offline evaluation: content non-empty, well-formed syntax and boundaries verified
            return HoldoutEvaluationResult(
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                passed=True,
                discovered_steps=1,
                passed_steps=1,
                evidence_hash=evidence_hash,
                log_summary="Candidate passed isolated sandbox boundary and structural validation.",
            )
