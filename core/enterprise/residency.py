"""Data Residency Enforcer and PII Sanitization for Enterprise Scope (HF-24).

Implements deterministic regex-based detection and redaction of PII (CPFs, CNPJs,
credentials, credit cards, emails) and fail-closed routing enforcement.
"""

from __future__ import annotations

import re
from typing import Tuple

from core.evolution.models import SecurityViolationError
from core.enterprise.models import DataResidencyMode, EnterpriseProjectConfig

# Precompiled deterministic regex filters for PII and secrets
CPF_PATTERN = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
CNPJ_PATTERN = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")
API_KEY_PATTERN = re.compile(r"\b(?:sk-[a-zA-Z0-9_\-]{20,}|ghp_[a-zA-Z0-9]{20,}|Bearer\s+[a-zA-Z0-9._\-]{20,})\b")


class PIISanitizer:
    """Detects and masks personally identifiable information and secrets."""

    @classmethod
    def contains_pii(cls, text: str) -> bool:
        """Return True if any forbidden PII or credential pattern is detected."""
        if not text:
            return False
        return bool(
            CPF_PATTERN.search(text)
            or CNPJ_PATTERN.search(text)
            or CREDIT_CARD_PATTERN.search(text)
            or API_KEY_PATTERN.search(text)
            or EMAIL_PATTERN.search(text)
        )

    @classmethod
    def sanitize(cls, text: str) -> Tuple[str, int]:
        """Replace detected PII with deterministic redaction tokens.

        Returns:
            Tuple of (sanitized_text, total_redactions_performed)
        """
        if not text:
            return "", 0

        count = 0
        cleaned, n = CPF_PATTERN.subn("[REDACTED_CPF]", text)
        count += n
        cleaned, n = CNPJ_PATTERN.subn("[REDACTED_CNPJ]", cleaned)
        count += n
        cleaned, n = CREDIT_CARD_PATTERN.subn("[REDACTED_CREDIT_CARD]", cleaned)
        count += n
        cleaned, n = API_KEY_PATTERN.subn("[REDACTED_SECRET]", cleaned)
        count += n
        cleaned, n = EMAIL_PATTERN.subn("[REDACTED_EMAIL]", cleaned)
        count += n

        return cleaned, count


class DataResidencyEnforcer:
    """Enforces geographic and provider residency policies for enterprise tenants."""

    LOCAL_ALLOWED_PROVIDERS = {
        "ollama_local",
        "ollama",
        "local",
        "qwen-fast",
        "qwen-deep",
        "local_zero",
    }

    EU_ALLOWED_PROVIDERS = {
        "ollama_local",
        "ollama",
        "local",
        "hetzner_eu",
        "falkenstein_vps",
        "mistral_eu",
    }

    def __init__(self, config: EnterpriseProjectConfig) -> None:
        self.config = config

    def validate_inference_route(self, provider: str, payload_text: str = "") -> bool:
        """Verify if target provider satisfies the project's data residency constraints.

        Raises SecurityViolationError if the route violates residency boundaries.
        """
        if not self.config.enabled:
            return True

        norm_prov = provider.strip().lower()

        # Strict PII check before any cloud transmission
        if self.config.pii_sanitization_strict and payload_text:
            if PIISanitizer.contains_pii(payload_text):
                raise SecurityViolationError(
                    f"PII containment violation: payload contains unmasked personal data. "
                    f"Sanitize payload before routing."
                )

        mode = self.config.residency_mode

        if mode == DataResidencyMode.LOCAL_ONLY:
            if norm_prov not in self.LOCAL_ALLOWED_PROVIDERS:
                raise SecurityViolationError(
                    f"Data residency violation: Project '{self.config.project_id}' is configured for "
                    f"LOCAL_ONLY residency. Public cloud provider '{provider}' is strictly blocked."
                )

        elif mode == DataResidencyMode.EU_ONLY:
            if norm_prov not in self.EU_ALLOWED_PROVIDERS:
                raise SecurityViolationError(
                    f"Data residency violation: Project '{self.config.project_id}' is configured for "
                    f"EU_ONLY residency. Provider '{provider}' outside authorized EU sovereign zone is blocked."
                )

        elif mode == DataResidencyMode.HYBRID_ENCRYPTED:
            # Requires strict payload sanitation
            if payload_text and PIISanitizer.contains_pii(payload_text):
                raise SecurityViolationError(
                    f"Data residency violation: HYBRID_ENCRYPTED mode requires all PII to be sanitized."
                )

        return True
