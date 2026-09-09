"""Opt-in, read-only service and Git observations for the HF-01 baseline."""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .baseline_models import ValidationMode, _aware_utc, _non_blank, utc_now

logger = logging.getLogger(__name__)
MAX_PROBE_BYTES = 2 * 1024 * 1024


class ProbeStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"
    REDIRECT = "redirect"
    RESPONSE_TOO_LARGE = "response_too_large"
    TOO_LARGE = "response_too_large"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    INVALID_CONFIG = "invalid_config"


class ProbeSpec(BaseModel):
    """A closed, credential-free definition of one optional GET probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    environment: str = Field(..., min_length=1)
    validation_mode: ValidationMode = ValidationMode.TARGET_ENVIRONMENT
    expected_origin: str | None = None
    require_connection: bool = False
    secret_ref: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_bytes: int = Field(default=MAX_PROBE_BYTES, gt=0, le=MAX_PROBE_BYTES)

    @field_validator("probe_id", "item_id", "environment", "secret_ref")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _non_blank(value) if value is not None else None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("probe url must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("probe url must not contain credentials")
        query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query)}
        if query_keys & {"token", "key", "secret", "password", "api_key", "access_token"}:
            raise ValueError("probe url must not contain credential query parameters")
        return value.strip()

    @field_validator("expected_origin")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("expected_origin must contain only scheme and authority")
        return f"{parsed.scheme}://{parsed.netloc}"


class ProbeObservation(BaseModel):
    """Sanitized probe result; raw payload and authorization are never retained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    origin: str = Field(..., min_length=1)
    status: ProbeStatus
    status_code: int | None = Field(default=None, ge=100, le=599)
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response_size: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    environment: str = Field(..., min_length=1)
    validation_mode: ValidationMode = ValidationMode.TARGET_ENVIRONMENT

    @field_validator("probe_id", "item_id", "origin", "environment", "error_code")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _non_blank(value) if value is not None else None

    _validate_time = field_validator("observed_at")(_aware_utc)


@dataclass(frozen=True)
class ProbeResponse:
    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class GetTransport(Protocol):
    def __call__(self, url: str, timeout_seconds: float, max_bytes: int) -> ProbeResponse:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _urllib_get(url: str, timeout_seconds: float, max_bytes: int) -> ProbeResponse:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "DarkFac-baseline-probe"}, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout_seconds) as response:
        return ProbeResponse(
            status_code=int(response.status),
            body=response.read(max_bytes + 1),
            headers={str(key): str(value) for key, value in response.headers.items()},
        )


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _observation(spec: ProbeSpec, *, status: ProbeStatus, status_code: int | None = None, body: bytes | None = None, error_code: str | None = None) -> ProbeObservation:
    return ProbeObservation(
        probe_id=spec.probe_id,
        item_id=spec.item_id,
        origin=_origin(spec.url),
        status=status,
        status_code=status_code,
        response_sha256=hashlib.sha256(body).hexdigest() if body is not None else None,
        response_size=len(body) if body is not None else None,
        error_code=error_code,
        environment=spec.environment,
        validation_mode=spec.validation_mode,
    )


def probe_endpoint(spec: ProbeSpec, transport: GetTransport | None = None) -> ProbeObservation:
    """Perform one bounded GET and return only sanitized metadata."""

    if spec.expected_origin and spec.expected_origin != _origin(spec.url):
        return _observation(spec, status=ProbeStatus.INVALID_CONFIG, error_code="PROBE_ORIGIN_MISMATCH")
    try:
        response = (transport or _urllib_get)(spec.url, spec.timeout_seconds, spec.max_bytes)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            status = ProbeStatus.REDIRECT
        elif exc.code in {401, 403}:
            status = ProbeStatus.UNAUTHORIZED
        elif exc.code == 404:
            status = ProbeStatus.NOT_FOUND
        else:
            status = ProbeStatus.HTTP_ERROR
        return _observation(spec, status=status, status_code=exc.code, error_code=f"HTTP_{exc.code}")
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        status = ProbeStatus.TIMEOUT if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) else ProbeStatus.NETWORK_ERROR
        return _observation(spec, status=status, error_code=status.value.upper())
    except (ValueError, OSError) as exc:
        logger.debug("Probe failed without exposing endpoint details: %s", type(exc).__name__)
        return _observation(spec, status=ProbeStatus.NETWORK_ERROR, error_code="PROBE_TRANSPORT_ERROR")

    if 300 <= response.status_code < 400:
        return _observation(spec, status=ProbeStatus.REDIRECT, status_code=response.status_code, error_code="HTTP_REDIRECT")
    if response.status_code in {401, 403}:
        return _observation(spec, status=ProbeStatus.UNAUTHORIZED, status_code=response.status_code, error_code=f"HTTP_{response.status_code}")
    if response.status_code == 404:
        return _observation(spec, status=ProbeStatus.NOT_FOUND, status_code=404, error_code="HTTP_404")
    if len(response.body) > spec.max_bytes:
        return _observation(spec, status=ProbeStatus.RESPONSE_TOO_LARGE, status_code=response.status_code, error_code="PROBE_RESPONSE_TOO_LARGE")
    if 200 <= response.status_code < 300:
        return _observation(spec, status=ProbeStatus.OK, status_code=response.status_code, body=response.body)
    return _observation(spec, status=ProbeStatus.HTTP_ERROR, status_code=response.status_code, error_code=f"HTTP_{response.status_code}")


def collect_probe_observations(specs: list[ProbeSpec] | tuple[ProbeSpec, ...], *, transport: GetTransport | None = None) -> tuple[ProbeObservation, ...]:
    observations = [probe_endpoint(spec, transport) for spec in sorted(specs, key=lambda item: item.probe_id)]
    return deduplicate_observations(observations)


def deduplicate_observations(observations: list[ProbeObservation] | tuple[ProbeObservation, ...]) -> tuple[ProbeObservation, ...]:
    seen: dict[tuple[Any, ...], ProbeObservation] = {}
    for observation in observations:
        key = (observation.probe_id, observation.item_id, observation.origin, observation.status.value, observation.status_code, observation.response_sha256, observation.error_code, observation.environment, observation.validation_mode.value)
        seen.setdefault(key, observation)
    return tuple(sorted(seen.values(), key=lambda item: (item.probe_id, item.observed_at)))


class GitReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(..., min_length=3)
    number: int = Field(..., gt=0)
    head_sha: str = Field(..., pattern=r"^[0-9a-f]{7,64}$")
    base_sha: str = Field(..., pattern=r"^[0-9a-f]{7,64}$")
    state: str = Field(..., min_length=1)
    check_conclusions: tuple[str, ...] = ()

    @field_validator("repository", "state")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_blank(value)


def parse_git_receipt(payload: Mapping[str, Any]) -> GitReceipt:
    """Parse only immutable PR/check metadata; ignore raw API fields."""

    head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
    base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
    checks = payload.get("checks", ())
    conclusions = [check["conclusion"].strip().lower() for check in checks if isinstance(check, Mapping) and isinstance(check.get("conclusion"), str)] if isinstance(checks, list) else []
    return GitReceipt(
        repository=str(payload.get("repository", "")),
        number=int(payload.get("number", 0)),
        head_sha=str(payload.get("head_sha") or head.get("sha") or "").lower(),
        base_sha=str(payload.get("base_sha") or base.get("sha") or "").lower(),
        state=str(payload.get("state", "")),
        check_conclusions=tuple(sorted(conclusions)),
    )


def deduplicate_git_receipts(receipts: list[GitReceipt] | tuple[GitReceipt, ...]) -> tuple[GitReceipt, ...]:
    unique = {(receipt.repository, receipt.number, receipt.head_sha): receipt for receipt in receipts}
    return tuple(unique[key] for key in sorted(unique))


def load_probe_config(path: str | Any) -> tuple[ProbeSpec, ...]:
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "1" or not isinstance(raw.get("probes"), list):
        raise ValueError("PROBE_CONFIG_SCHEMA_CHANGED")
    return tuple(ProbeSpec.model_validate(item) for item in raw["probes"])


__all__ = ["GitReceipt", "GetTransport", "ProbeObservation", "ProbeResponse", "ProbeSpec", "ProbeStatus", "collect_probe_observations", "deduplicate_git_receipts", "deduplicate_observations", "load_probe_config", "parse_git_receipt", "probe_endpoint"]
