"""Safe export and diagnostic sanitization for workflow manifests and contracts.

Guarantees and Scope:
- Preserves SecretReference metadata (ref_id, provider, locator, variable_name) as secure pointers without transporting secret values.
- Sanitizes endpoints and services, ensuring no user credentials, DSNs, or query tokens are exposed.
- Formats Pydantic validation errors into clean diagnostics containing only error code, field location ('loc'), and sanitized message, completely suppressing the raw 'input' / 'input_value'.
- Explicit Limitation: This module does not inspect real secret manager values and does not promise universal secret detection across arbitrary unstructured prose.
"""

from __future__ import annotations

import re
from typing import Any
from pydantic import BaseModel, ValidationError

from core.workflow.contracts import EnvironmentManifest, _looks_like_secret_value


def _sanitize_text(text: str) -> str:
    """Censor secret patterns, DSNs, and sensitive query parameters from text."""
    cleaned = re.sub(
        r"(?:password|passwd|token|api[_-]?key|secret|credential)\s*[:=]\s*[^\s]+",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"postgres(?:ql)?://[^\s]+", "[REDACTED_DSN]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?<=[?&])(?:token|access_token|key|api_key|secret|password)=[^&\s]+",
        "[REDACTED_PARAM]",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def safe_validation_diagnostics(exc: ValidationError) -> list[dict[str, Any]]:
    """Convert a Pydantic ValidationError into safe diagnostic dictionaries.

    Returns a list of dicts with:
    - 'code': the error type/code (e.g. 'value_error')
    - 'loc': list representing field location (e.g. ['endpoints', 0, 'url'])
    - 'message': sanitized error message without raw input values or secret tokens

    Crucially, 'input' and 'input_value' are strictly omitted to prevent secret leaks.
    """
    diagnostics: list[dict[str, Any]] = []
    for err in exc.errors():
        code = err.get("type", "validation_error")
        loc = list(err.get("loc", ()))
        msg = err.get("msg", "")
        sanitized_msg = _sanitize_text(msg)
        diagnostics.append({
            "code": code,
            "loc": loc,
            "message": sanitized_msg,
        })
    return diagnostics


def format_safe_validation_error(exc: ValidationError) -> str:
    """Render a human-readable safe summary of a ValidationError without leaking inputs."""
    diagnostics = safe_validation_diagnostics(exc)
    lines: list[str] = []
    for diag in diagnostics:
        loc_str = " -> ".join(str(item) for item in diag["loc"]) or "root"
        lines.append(f"[VALIDATION_ERROR] loc='{loc_str}' code='{diag['code']}': {diag['message']}")
    return "\n".join(lines)


def safe_export_model(data: Any) -> Any:
    """Recursively sanitize a dictionary, list, or Pydantic model for safe export.

    Redacts keys or values that match credential or secret heuristics.
    """
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")

    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in {"password", "passwd", "token", "secret", "api_key", "apikey", "credential"}:
                sanitized[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                sanitized[k] = safe_export_model(v)
            elif isinstance(v, str) and _looks_like_secret_value(v):
                sanitized[k] = _sanitize_text(v)
            else:
                sanitized[k] = v
        return sanitized

    if isinstance(data, list):
        return [safe_export_model(item) for item in data]

    if isinstance(data, str) and _looks_like_secret_value(data):
        return _sanitize_text(data)

    return data


def safe_export_manifest(manifest: EnvironmentManifest) -> dict[str, Any]:
    """Export an EnvironmentManifest ensuring secrets and credentials are not exposed."""
    raw = manifest.model_dump(mode="json")
    return safe_export_model(raw)
