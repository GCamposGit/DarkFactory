"""Continuous Negative Acceptance Observer for Autonomous Execution.

Normative implementation of:
- docs/handoffs/continuous-autonomy/HF-15-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- docs/handoffs/continuous-autonomy/acceptance-protocol.json

This observer is 100% read-only and strictly decoupled from execution engines,
controllers, and persistence mutations. It evaluates execution contexts, traces,
and logs against the 13 invariant negative acceptance rules (V01-V13).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field

from core.workflow.control_contracts import JobKey

logger = logging.getLogger("darkfac.acceptance.continuous_observer")

CANONICAL_PROTOCOL_PATH = Path("docs/handoffs/continuous-autonomy/acceptance-protocol.json")

# Fallback definition in case external JSON file is unavailable
BUILTIN_RULES: dict[str, dict[str, Any]] = {
    "V01": {
        "rule_id": "V01",
        "name": "consumer_off_health200_rejected",
        "severity": "critical",
        "description": "Consumidor inativo ou desligado com endpoint de health retornando HTTP 200 deve ser reprovado para evitar falsos positivos de disponibilidade.",
        "fail_closed": True,
        "remediation": "Verificar status real de execução do worker/consumidor nos probes antes de emitir status HTTP 200 ou marcar o componente como degradado/unhealthy.",
    },
    "V02": {
        "rule_id": "V02",
        "name": "manual_stage_advance_rejected",
        "severity": "critical",
        "description": "Tentativa do controlador ou processo automatizado de invocar ou avançar diretamente fases manuais reservadas a humanos deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Pausar a execução em WAITING_HUMAN e exigir recibo formal de aprovação assinado pelo Owner antes de avançar fases manuais.",
    },
    "V03": {
        "rule_id": "V03",
        "name": "self_served_evidence_rejected",
        "severity": "critical",
        "description": "Evidência produzida ou atestada unicamente pelo próprio candidato sob teste é inválida e deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Exigir oráculo auditor independente ou probe externo qualificado com identidade sanitizada distinta para emitir o EvidenceReceipt.",
    },
    "V04": {
        "rule_id": "V04",
        "name": "anomalous_clock_rejected",
        "severity": "critical",
        "description": "Timestamp futuro, regressivo ou desvio de relógio além da tolerância máxima configurada deve ser reprovado.",
        "fail_closed": True,
        "remediation": "Sincronizar o relógio do sistema via NTP e rejeitar eventos com timestamps fora da janela de tolerância temporal estrita.",
    },
    "V05": {
        "rule_id": "V05",
        "name": "expired_lease_claim_rejected",
        "severity": "critical",
        "description": "Claim com lease vencido ou fencing token defasado não produz efeito e qualquer mutação tentada sob ele deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Renovar o lease antes da expiração através de batimentos de heartbeat ou abortar a operação e devolver o job ao ControlStore.",
    },
    "V06": {
        "rule_id": "V06",
        "name": "idempotency_payload_mismatch_rejected",
        "severity": "critical",
        "description": "Resubmissão com a mesma chave de idempotência mas com payload ou digest divergente do original deve ser reprovada com IdempotencyConflict.",
        "fail_closed": True,
        "remediation": "Reenviar exatamente o mesmo payload ou emitir um novo comando com nova chave de identificação/iteração.",
    },
    "V07": {
        "rule_id": "V07",
        "name": "credential_leak_in_logs_rejected",
        "severity": "critical",
        "description": "Presença de segredos, chaves de API, senhas, tokens privados ou credenciais em texto claro em logs ou payloads deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Substituir credenciais por referências opacas (SecretReference), mascarar saídas sensíveis e expurgar dados confidenciais dos streams de log.",
    },
    "V08": {
        "rule_id": "V08",
        "name": "missing_independent_oracle_rejected",
        "severity": "critical",
        "description": "Falta de sujeito auditor distinto ou oráculo independente formalmente designado para validação e revisão deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Designar e vincular um oráculo de validação com identidade independente e distinta do sujeito executor antes da emissão de resultados.",
    },
    "V09": {
        "rule_id": "V09",
        "name": "timeout_without_checkpoint_rejected",
        "severity": "critical",
        "description": "Ocorrência de timeout em job ou estágio sem o devido registro prévio de checkpoint de progresso deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Persistir checkpoints incrementais periódicos durante a execução para viabilizar recuperação determinística e auditoria de progresso.",
    },
    "V10": {
        "rule_id": "V10",
        "name": "resource_starvation_rejected",
        "severity": "critical",
        "description": "Esgotamento de recursos, esgotamento de quota ou rate-limiting sem política ativa de backoff ou failover deve ser reprovado.",
        "fail_closed": True,
        "remediation": "Aplicar política de backoff com jitter, chaveamento para modelos/hosts locais de custo zero ou redução de concorrência ativa.",
    },
    "V11": {
        "rule_id": "V11",
        "name": "out_of_bounds_mutation_rejected",
        "severity": "critical",
        "description": "Tentativa ou ocorrência de alteração de arquivos ou recursos fora do conjunto restrito de caminhos permitidos (allowed_paths) deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Reverter modificações que excedam os caminhos permitidos e restringir a escrita exclusivamente aos arquivos delimitados no contrato da tarefa.",
    },
    "V12": {
        "rule_id": "V12",
        "name": "heartbeat_silence_window_rejected",
        "severity": "critical",
        "description": "Janela de silêncio de heartbeat além do limite tolerado de 45 segundos deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Emitir batimentos cardíacos (heartbeats) em intervalos regulares de 10 segundos para manter o lease ativo e atestar vivacidade.",
    },
    "V13": {
        "rule_id": "V13",
        "name": "dirty_worktree_rejected",
        "severity": "critical",
        "description": "Execução com worktree contendo alterações não comitadas, arquivos não rastreados não permitidos ou baseline incorreta deve ser reprovada.",
        "fail_closed": True,
        "remediation": "Garantir worktree limpa (git status clean) e sincronizada com o commit SHA de baseline antes de autorizar qualquer execução de workflow.",
    },
}

CREDENTIAL_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{30,}", re.IGNORECASE),
    re.compile(r"gho_[A-Za-z0-9]{30,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"bearer\s+[A-Za-z0-9_\-\.]{25,}", re.IGNORECASE),
    re.compile(
        r"(?:password|passwd|api_key|apikey|secret_key|private_key|auth_token)\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{8,}[\"']?",
        re.IGNORECASE,
    ),
    re.compile(r"DARKFAC_SECRET_[A-Za-z0-9_]+", re.IGNORECASE),
]


class RuleDefinition(BaseModel):
    """Specification of an invariant negative rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    severity: str = "critical"
    description: str = Field(min_length=1)
    fail_closed: bool = True
    remediation: str = Field(min_length=1)


class RuleViolation(BaseModel):
    """Structured evidence of a negative acceptance invariant violation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1)
    rule_name: str = Field(min_length=1)
    severity: str = "critical"
    message: str = Field(min_length=1)
    remediation: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CorrelationResult(BaseModel):
    """Verification result of run_id, JobKey and nonce correlation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(default="")
    job_key: Optional[str] = None
    nonce: str = Field(default="")
    correlated: bool
    error: Optional[str] = None


class AuditReport(BaseModel):
    """Consolidated immutable audit report emitted by the ContinuousObserver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    passed: bool
    violations: list[RuleViolation] = Field(default_factory=list)
    evaluated_rules: list[str] = Field(default_factory=list)
    audited_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation: Optional[CorrelationResult] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContinuousObserver:
    """Read-only observer for invariant negative acceptance verification.

    Governed by:
    - 100% read-only operation.
    - Decoupled from controllers and executors.
    - Performs NO mutations, workflow state transitions, or writes to production databases.
    - Correlates run_id, JobKey, and nonce.
    - Audits contexts and logs against negative acceptance rules V01-V13.
    """

    def __init__(
        self,
        protocol_path: Optional[Path] = None,
        custom_rules: Optional[Sequence[RuleDefinition | dict[str, Any]]] = None,
    ) -> None:
        self._rules: dict[str, RuleDefinition] = {}
        self._protocol_path = protocol_path or CANONICAL_PROTOCOL_PATH

        if custom_rules:
            for r in custom_rules:
                rule_def = r if isinstance(r, RuleDefinition) else RuleDefinition(**r)
                self._rules[rule_def.rule_id] = rule_def
        else:
            loaded = False
            if self._protocol_path.exists():
                try:
                    content = json.loads(self._protocol_path.read_text(encoding="utf-8"))
                    for r_data in content.get("rules", []):
                        r_def = RuleDefinition(**r_data)
                        self._rules[r_def.rule_id] = r_def
                    loaded = True
                except Exception as exc:
                    logger.warning("Failed to load protocol from %s: %s", self._protocol_path, exc)

            if not loaded:
                for r_data in BUILTIN_RULES.values():
                    r_def = RuleDefinition(**r_data)
                    self._rules[r_def.rule_id] = r_def

        # Lock down instance immutability
        self._frozen = True

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError(
                f"ContinuousObserver is strictly read-only and immutable; cannot set attribute '{name}'"
            )
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            f"ContinuousObserver is strictly read-only and immutable; cannot delete attribute '{name}'"
        )

    @property
    def rules(self) -> dict[str, RuleDefinition]:
        """Expose rule definitions as an immutable view."""
        return dict(self._rules)

    @property
    def protocol_path(self) -> Path:
        return self._protocol_path

    # -------------------------------------------------------------------------
    # Correlation Verification
    # -------------------------------------------------------------------------

    def correlate(
        self,
        run_id: str,
        job_key: Union[JobKey, dict[str, Any], str],
        nonce: str,
    ) -> CorrelationResult:
        """Correlate run_id, JobKey, and nonce for auditable provenance.

        Invariants:
        - run_id must be a non-empty string.
        - nonce must be non-empty and well-formed (at least 4 characters).
        - job_key must possess a run_id attribute matching the given run_id.
        """
        if not run_id or not isinstance(run_id, str) or not run_id.strip():
            return CorrelationResult(
                run_id=str(run_id),
                job_key=str(job_key),
                nonce=str(nonce),
                correlated=False,
                error="Invalid or empty run_id",
            )

        if not nonce or not isinstance(nonce, str) or len(nonce.strip()) < 4:
            return CorrelationResult(
                run_id=run_id,
                job_key=str(job_key),
                nonce=str(nonce),
                correlated=False,
                error="Invalid or empty nonce (minimum 4 characters required)",
            )

        extracted_run_id: Optional[str] = None
        canonical_key_str: str = ""

        if isinstance(job_key, JobKey):
            extracted_run_id = job_key.run_id
            canonical_key_str = job_key.canonical_key()
        elif isinstance(job_key, dict):
            extracted_run_id = job_key.get("run_id")
            canonical_key_str = str(job_key)
        elif isinstance(job_key, str):
            canonical_key_str = job_key
            parts = job_key.split(":")
            if parts:
                extracted_run_id = parts[0]
        else:
            return CorrelationResult(
                run_id=run_id,
                job_key=str(job_key),
                nonce=nonce,
                correlated=False,
                error=f"Unsupported job_key type: {type(job_key).__name__}",
            )

        if extracted_run_id != run_id:
            return CorrelationResult(
                run_id=run_id,
                job_key=canonical_key_str,
                nonce=nonce,
                correlated=False,
                error=(
                    f"JobKey run_id '{extracted_run_id}' does not match "
                    f"context run_id '{run_id}'"
                ),
            )

        return CorrelationResult(
            run_id=run_id,
            job_key=canonical_key_str,
            nonce=nonce,
            correlated=True,
            error=None,
        )

    # -------------------------------------------------------------------------
    # Individual Rule Auditing (V01-V13)
    # -------------------------------------------------------------------------

    def verify_rule(
        self,
        rule_id: str,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]] = None,
    ) -> Optional[RuleViolation]:
        """Verify an execution context and logs against a single negative rule.

        Returns RuleViolation if a violation is detected, or None if the invariant holds.
        Operates strictly read-only without modifying inputs.
        """
        rule_def = self._rules.get(rule_id)
        if not rule_def:
            raise KeyError(f"Unknown rule_id: '{rule_id}'")

        handler_name = f"_check_{rule_id.lower()}"
        handler = getattr(self, handler_name, None)
        if not handler:
            raise NotImplementedError(f"No verification handler implemented for rule {rule_id}")

        return handler(rule_def, context, logs)

    # -------------------------------------------------------------------------
    # Full Run Auditing
    # -------------------------------------------------------------------------

    def audit_run(
        self,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]] = None,
        now: Optional[datetime] = None,
    ) -> AuditReport:
        """Audits an entire run context and log trace against all 13 rules (V01-V13).

        Does not mutate the context or logs.
        Returns an AuditReport.
        """
        audited_at = now or datetime.now(UTC)
        run_id = str(context.get("run_id", "unknown_run"))

        # Provenance correlation check if job_key and nonce are provided
        correlation_result: Optional[CorrelationResult] = None
        if "job_key" in context and "nonce" in context:
            correlation_result = self.correlate(
                run_id=run_id,
                job_key=context["job_key"],
                nonce=context["nonce"],
            )

        violations: list[RuleViolation] = []
        evaluated_rules: list[str] = sorted(self._rules.keys())

        # If correlation was evaluated and failed, record a provenance violation
        if correlation_result and not correlation_result.correlated:
            violations.append(
                RuleViolation(
                    rule_id="V06",  # Idempotency / provenance divergence
                    rule_name="provenance_correlation_mismatch",
                    severity="critical",
                    message=f"Provenance correlation failure: {correlation_result.error}",
                    remediation="Correlacionar estritamente run_id, JobKey e nonce antes da execução.",
                    details={"correlation": correlation_result.model_dump()},
                    timestamp=audited_at,
                )
            )

        for rule_id in evaluated_rules:
            violation = self.verify_rule(rule_id, context, logs)
            if violation and violation not in violations:
                violations.append(violation)

        passed = len(violations) == 0
        return AuditReport(
            run_id=run_id,
            passed=passed,
            violations=violations,
            evaluated_rules=evaluated_rules,
            audited_at=audited_at,
            correlation=correlation_result,
            metadata={"total_evaluated": len(evaluated_rules)},
        )

    # -------------------------------------------------------------------------
    # Rule Handlers
    # -------------------------------------------------------------------------

    def _check_v01(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V01: consumer_off_health200_rejected."""
        if context.get("consumer_off_health200_rejected") is True or context.get("consumer_off_health200") is True:
            return self._build_violation(rule_def, "Consumer is stopped but health check reported HTTP 200", context)

        consumer_status = str(context.get("consumer_status", "")).lower()
        consumer_active = context.get("consumer_active")
        worker_running = context.get("worker_running")
        consumer_running = context.get("consumer_running")

        is_off = (
            consumer_active is False
            or worker_running is False
            or consumer_running is False
            or consumer_status in ("off", "inactive", "stopped", "down", "offline", "killed")
        )

        health_code = context.get("health_status_code") or context.get("health_code") or context.get("health_status")
        health_ok = context.get("health_ok") is True or health_code in (200, "200", "healthy", "ok", "up")

        if is_off and health_ok:
            return self._build_violation(
                rule_def,
                f"Consumer is inactive/off (status={consumer_status!r}, active={consumer_active}) but health endpoint returned healthy/200",
                {"consumer_status": consumer_status, "health_code": health_code},
            )
        return None

    def _check_v02(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V02: manual_stage_advance_rejected."""
        if context.get("manual_stage_advance_rejected") is True or context.get("manual_stage_advance") is True:
            return self._build_violation(rule_def, "Automated controller attempted to advance a manual stage", context)

        stage = str(context.get("stage", "")).lower()
        is_manual = (
            context.get("is_manual_stage") is True
            or context.get("stage_type") == "manual"
            or stage in ("human_intake", "manual_approval", "grill_human", "release_promotion", "manual")
        )

        invoked_by = str(context.get("invoked_by", "")).lower()
        controller_invoked = (
            invoked_by in ("controller", "system", "auto", "automated_engine")
            or context.get("direct_advance") is True
            or context.get("controller_invoked_manual_stage") is True
        )

        has_approval = (
            context.get("human_approved") is True
            or context.get("has_human_approval") is True
            or context.get("owner_approval_receipt") is not None
        )

        if is_manual and controller_invoked and not has_approval:
            return self._build_violation(
                rule_def,
                f"Automated controller ({invoked_by or 'automated'}) attempted to advance manual stage '{stage}' without formal human approval",
                {"stage": stage, "invoked_by": invoked_by},
            )
        return None

    def _check_v03(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V03: self_served_evidence_rejected."""
        if context.get("self_served_evidence_rejected") is True or context.get("self_served_evidence") is True:
            return self._build_violation(rule_def, "Evidence produced solely by candidate under test", context)

        candidate = str(context.get("candidate_identity") or context.get("candidate") or "").strip()
        producer = str(context.get("evidence_producer") or context.get("auditor_identity") or "").strip()

        evidence = context.get("evidence")
        if isinstance(evidence, dict):
            if evidence.get("self_served") is True:
                return self._build_violation(rule_def, "Evidence marked self-served by candidate", evidence)
            ev_producer = str(evidence.get("producer") or evidence.get("auditor") or "").strip()
            if candidate and ev_producer and candidate == ev_producer:
                return self._build_violation(
                    rule_def,
                    f"Evidence producer '{ev_producer}' is identical to candidate '{candidate}'",
                    {"candidate": candidate, "producer": ev_producer},
                )

        if candidate and producer and candidate == producer:
            return self._build_violation(
                rule_def,
                f"Auditor/evidence producer identity '{producer}' matches candidate identity '{candidate}'",
                {"candidate": candidate, "producer": producer},
            )
        return None

    def _check_v04(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V04: anomalous_clock_rejected."""
        if context.get("anomalous_clock_rejected") is True or context.get("anomalous_clock") is True:
            return self._build_violation(rule_def, "Anomalous clock detected", context)

        max_skew = float(context.get("max_skew_seconds", 30.0))
        clock_skew = float(context.get("clock_skew_seconds", 0.0))
        if abs(clock_skew) > max_skew:
            return self._build_violation(
                rule_def,
                f"Clock skew of {clock_skew}s exceeds tolerance limit of {max_skew}s",
                {"clock_skew_seconds": clock_skew, "max_skew_seconds": max_skew},
            )

        ts_raw = context.get("timestamp") or context.get("observed_at")
        if ts_raw:
            ts: Optional[datetime] = None
            if isinstance(ts_raw, datetime):
                ts = ts_raw
            elif isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    pass

            if ts:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                # Allow up to max_skew into the future
                if ts > now + timedelta(seconds=max_skew):
                    skew = (ts - now).total_seconds()
                    return self._build_violation(
                        rule_def,
                        f"Future timestamp detected ({ts.isoformat()}), skewing {skew:.1f}s into future",
                        {"timestamp": ts.isoformat(), "observer_now": now.isoformat()},
                    )
        return None

    def _check_v05(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V05: expired_lease_claim_rejected."""
        if (
            context.get("expired_lease_claim_rejected") is True
            or context.get("lease_expired") is True
            or context.get("claim_expired") is True
            or context.get("stale_fencing_token") is True
        ):
            return self._build_violation(rule_def, "Claim lease is expired or fencing token is stale", context)

        claim_data = context.get("claim")
        expires_at_raw = None
        if isinstance(claim_data, dict):
            expires_at_raw = claim_data.get("expires_at")
        elif hasattr(claim_data, "expires_at"):
            expires_at_raw = getattr(claim_data, "expires_at")
        else:
            expires_at_raw = context.get("expires_at")

        if expires_at_raw:
            exp_dt: Optional[datetime] = None
            if isinstance(expires_at_raw, datetime):
                exp_dt = expires_at_raw
            elif isinstance(expires_at_raw, str):
                try:
                    exp_dt = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    pass

            if exp_dt:
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                if now > exp_dt:
                    return self._build_violation(
                        rule_def,
                        f"Claim lease expired at {exp_dt.isoformat()}, current observer time is {now.isoformat()}",
                        {"expires_at": exp_dt.isoformat(), "now": now.isoformat()},
                    )
        return None

    def _check_v06(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V06: idempotency_payload_mismatch_rejected."""
        if (
            context.get("idempotency_payload_mismatch_rejected") is True
            or context.get("idempotency_conflict") is True
            or context.get("payload_mismatch") is True
        ):
            return self._build_violation(rule_def, "Idempotency conflict: key resubmitted with mismatched payload", context)

        orig_digest = context.get("existing_payload_digest") or context.get("original_payload_digest")
        new_digest = context.get("new_payload_digest") or context.get("payload_digest")

        if orig_digest and new_digest and orig_digest != new_digest:
            return self._build_violation(
                rule_def,
                f"Idempotency key resubmitted with conflicting digest: original={orig_digest}, new={new_digest}",
                {"original_digest": orig_digest, "new_digest": new_digest},
            )
        return None

    def _check_v07(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V07: credential_leak_in_logs_rejected."""
        if context.get("credential_leak_in_logs_rejected") is True or context.get("credential_leak") is True:
            return self._build_violation(rule_def, "Credential leak detected in logs or execution context", context)

        # Inspect log sequences and context string fields
        text_to_scan: list[str] = []
        if logs:
            if isinstance(logs, str):
                text_to_scan.append(logs)
            else:
                text_to_scan.extend(str(line) for line in logs)

        if "logs" in context:
            c_logs = context["logs"]
            if isinstance(c_logs, str):
                text_to_scan.append(c_logs)
            elif isinstance(c_logs, (list, tuple)):
                text_to_scan.extend(str(line) for line in c_logs)

        # Also scan string values in context
        for k, v in context.items():
            if isinstance(v, str) and k not in ("run_id", "nonce", "job_key", "baseline_sha"):
                text_to_scan.append(v)

        combined_text = "\n".join(text_to_scan)
        for pattern in CREDENTIAL_PATTERNS:
            match = pattern.search(combined_text)
            if match:
                matched_snippet = match.group(0)
                redacted = matched_snippet[:4] + "***" + matched_snippet[-2:]
                return self._build_violation(
                    rule_def,
                    f"Cleartext secret/credential pattern detected in logs: {redacted}",
                    {"pattern": pattern.pattern, "leak_sample": redacted},
                )
        return None

    def _check_v08(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V08: missing_independent_oracle_rejected."""
        if (
            context.get("missing_independent_oracle_rejected") is True
            or context.get("missing_independent_oracle") is True
            or context.get("independent_oracle_present") is False
        ):
            return self._build_violation(rule_def, "Missing independent oracle for verification/validation", context)

        stage = str(context.get("stage", "")).lower()
        requires_oracle = (
            stage in ("validation", "independent_review", "verification", "acceptance")
            or context.get("requires_independent_oracle") is True
        )

        if requires_oracle:
            oracle_id = context.get("oracle_identity") or context.get("auditor_identity") or context.get("oracle")
            candidate_id = context.get("candidate_identity") or context.get("candidate")
            if not oracle_id:
                return self._build_violation(
                    rule_def,
                    f"Stage '{stage}' requires an independent oracle, but no oracle/auditor identity was provided",
                    {"stage": stage},
                )
            if candidate_id and oracle_id == candidate_id:
                return self._build_violation(
                    rule_def,
                    f"Stage '{stage}' oracle identity '{oracle_id}' cannot be identical to candidate identity",
                    {"stage": stage, "oracle": oracle_id, "candidate": candidate_id},
                )
        return None

    def _check_v09(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V09: timeout_without_checkpoint_rejected."""
        if (
            context.get("timeout_without_checkpoint_rejected") is True
            or context.get("timeout_without_checkpoint") is True
        ):
            return self._build_violation(rule_def, "Stage timed out without prior checkpoint persistence", context)

        timed_out = (
            context.get("timed_out") is True
            or context.get("timeout") is True
            or context.get("outcome") == "timeout"
        )
        has_checkpoint = (
            context.get("has_checkpoint") is True
            or int(context.get("checkpoints_count", 0)) > 0
            or bool(context.get("checkpoints"))
        )

        if timed_out and not has_checkpoint:
            return self._build_violation(
                rule_def,
                "Stage/Job timed out but zero progress checkpoints were persisted prior to failure",
                {"timed_out": True, "checkpoints_count": context.get("checkpoints_count", 0)},
            )
        return None

    def _check_v10(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V10: resource_starvation_rejected."""
        if (
            context.get("resource_starvation_rejected") is True
            or context.get("resource_starvation") is True
        ):
            return self._build_violation(rule_def, "Resource starvation encountered without mitigation policy", context)

        is_starved = (
            context.get("starvation") is True
            or context.get("rate_limited") is True
            or context.get("quota_exhausted") is True
            or context.get("slots_starved") is True
        )
        has_policy = (
            context.get("has_backoff_policy") is True
            or context.get("has_fallback") is True
            or context.get("policy_applied") is True
        )

        if is_starved and not has_policy:
            return self._build_violation(
                rule_def,
                "Resource starvation/rate-limit hit without an active backoff, jitter, or local fallback policy",
                {"is_starved": True, "has_policy": False},
            )
        return None

    def _check_v11(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V11: out_of_bounds_mutation_rejected."""
        if (
            context.get("out_of_bounds_mutation_rejected") is True
            or context.get("out_of_bounds_mutation") is True
        ):
            return self._build_violation(rule_def, "Mutation attempted on paths outside allowed_paths", context)

        mutations = (
            context.get("mutated_paths")
            or context.get("changed_files")
            or context.get("mutations")
        )
        allowed = context.get("allowed_paths") or context.get("permitted_paths")

        if mutations and allowed is not None:
            # Normalize paths to posix strings
            norm_allowed = {Path(p).as_posix().lower() for p in allowed}
            unauthorized: list[str] = []

            for m in mutations:
                norm_m = Path(m).as_posix().lower()
                # Check if exact match or child of allowed path
                allowed_match = any(
                    norm_m == a or norm_m.startswith(a.rstrip("/") + "/")
                    for a in norm_allowed
                )
                if not allowed_match:
                    unauthorized.append(m)

            if unauthorized:
                return self._build_violation(
                    rule_def,
                    f"Out of bounds mutations detected in unauthorized paths: {unauthorized}",
                    {"unauthorized_paths": unauthorized, "allowed_paths": list(allowed)},
                )
        return None

    def _check_v12(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V12: heartbeat_silence_window_rejected."""
        if (
            context.get("heartbeat_silence_window_rejected") is True
            or context.get("heartbeat_silence_rejected") is True
            or context.get("heartbeat_silence") is True
        ):
            return self._build_violation(rule_def, "Heartbeat silence window exceeded 45s threshold", context)

        silence_seconds = float(context.get("heartbeat_silence_seconds", 0.0))
        if silence_seconds > 45.0:
            return self._build_violation(
                rule_def,
                f"Heartbeat silence window of {silence_seconds:.1f}s exceeded maximum lease threshold of 45.0s",
                {"silence_seconds": silence_seconds, "limit_seconds": 45.0},
            )

        last_hb = context.get("last_heartbeat_at")
        if last_hb:
            hb_dt: Optional[datetime] = None
            if isinstance(last_hb, datetime):
                hb_dt = last_hb
            elif isinstance(last_hb, str):
                try:
                    hb_dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                except ValueError:
                    pass

            if hb_dt:
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                elapsed = (now - hb_dt).total_seconds()
                if elapsed > 45.0:
                    return self._build_violation(
                        rule_def,
                        f"Heartbeat silence of {elapsed:.1f}s since last heartbeat ({hb_dt.isoformat()}) exceeds 45s lease",
                        {"last_heartbeat_at": hb_dt.isoformat(), "elapsed_seconds": elapsed},
                    )
        return None

    def _check_v13(
        self,
        rule_def: RuleDefinition,
        context: dict[str, Any],
        logs: Optional[Union[Sequence[str], str]],
    ) -> Optional[RuleViolation]:
        """V13: dirty_worktree_rejected."""
        if (
            context.get("dirty_worktree_rejected") is True
            or context.get("dirty_worktree") is True
            or context.get("worktree_clean") is False
            or context.get("is_clean") is False
            or context.get("uncommitted_changes") is True
        ):
            return self._build_violation(rule_def, "Dirty worktree with uncommitted changes rejected", context)

        uncommitted = context.get("uncommitted_files")
        if uncommitted and len(uncommitted) > 0:
            return self._build_violation(
                rule_def,
                f"Worktree contains {len(uncommitted)} uncommitted files: {uncommitted}",
                {"uncommitted_files": uncommitted},
            )

        base_sha = context.get("baseline_sha")
        expected_base = context.get("expected_baseline_sha")
        if base_sha and expected_base and base_sha != expected_base:
            return self._build_violation(
                rule_def,
                f"Worktree baseline SHA '{base_sha}' diverges from expected baseline '{expected_base}'",
                {"baseline_sha": base_sha, "expected_baseline_sha": expected_base},
            )
        return None

    # -------------------------------------------------------------------------
    # Helper
    # -------------------------------------------------------------------------

    def _build_violation(
        self,
        rule_def: RuleDefinition,
        message: str,
        details: dict[str, Any],
    ) -> RuleViolation:
        # Sanitize details to avoid embedding un-serializable objects
        safe_details = {}
        for k, v in details.items():
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                safe_details[k] = v
            else:
                safe_details[k] = str(v)

        return RuleViolation(
            rule_id=rule_def.rule_id,
            rule_name=rule_def.name,
            severity=rule_def.severity,
            message=message,
            remediation=rule_def.remediation,
            details=safe_details,
        )
