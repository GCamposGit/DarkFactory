"""Fail-closed HTTPS transport primitives for the research clients.

The research integrations deliberately keep networking behind this module.  It
uses the platform trust store (or an explicitly configured CA bundle) and
never falls back to an unverified SSL context.
"""

from __future__ import annotations

import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Iterable, Mapping, TypeVar


class TransportErrorKind(str, Enum):
    """Stable categories for failures crossing the network boundary."""

    TLS_ERROR = "tls_error"
    TLS = "tls_error"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    TRANSPORT = "transport_error"
    HTTP_STATUS = "http_status"
    HTTP_ERROR = "http_status"
    INVALID_RESPONSE = "invalid_response"
    EMPTY_RESPONSE = "empty_response"


class SearchStatus(str, Enum):
    """Outcome of a research query, independent from its item count."""

    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"


class TransportFailure:
    """Safe, serializable description of an external transport failure.

    Messages are intentionally fixed and never include exception text, URLs,
    query strings, request headers, or credentials.
    """

    __slots__ = ("kind", "message", "status_code", "retryable")

    def __init__(
        self,
        kind: TransportErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    @property
    def category(self) -> TransportErrorKind:
        """Alias useful to callers that use category-oriented contracts."""

        return self.kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return (
            "TransportFailure("
            f"kind={self.kind.value!r}, status_code={self.status_code!r}, "
            f"retryable={self.retryable!r})"
        )


class TransportError(RuntimeError):
    """Exception raised by :class:`ResearchTransport` with a safe contract."""

    def __init__(self, failure: TransportFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class TlsConfigurationError(TransportError):
    """Raised when a trusted TLS context cannot be created."""


class TransportResponse:
    """Minimal response value returned by a successful HTTPS request."""

    __slots__ = ("status_code", "body", "headers")

    def __init__(
        self,
        status_code: int,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {})


T = TypeVar("T")


class ResearchSearchResult(list[T], Generic[T]):
    """List-compatible result with an explicit empty/failure distinction."""

    def __init__(
        self,
        items: Iterable[T] = (),
        *,
        status: SearchStatus | None = None,
        failure: TransportFailure | None = None,
    ) -> None:
        values = list(items)
        if status is None:
            status = SearchStatus.SUCCESS if values else SearchStatus.EMPTY
        if status == SearchStatus.FAILED and failure is None:
            raise ValueError("failed search results require a structured failure")
        if status != SearchStatus.FAILED and failure is not None:
            raise ValueError("only failed search results may contain a failure")
        super().__init__(values)
        self.status = status
        self.failure = failure

    @classmethod
    def empty(cls) -> "ResearchSearchResult[T]":
        return cls(status=SearchStatus.EMPTY)

    @classmethod
    def failed(cls, failure: TransportFailure) -> "ResearchSearchResult[T]":
        return cls(status=SearchStatus.FAILED, failure=failure)

    @property
    def is_empty(self) -> bool:
        return self.status == SearchStatus.EMPTY

    @property
    def is_failed(self) -> bool:
        return self.status == SearchStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "count": len(self),
            "failure": self.failure.to_dict() if self.failure else None,
        }


def _failure(
    kind: TransportErrorKind,
    message: str,
    *,
    status_code: int | None = None,
    retryable: bool = False,
) -> TransportError:
    return TransportError(
        TransportFailure(
            kind,
            message,
            status_code=status_code,
            retryable=retryable,
        )
    )


def get_ssl_context(ca_bundle: str | Path | None = None) -> ssl.SSLContext:
    """Create a certificate-verifying context or fail closed.

    ``DARKFAC_CA_BUNDLE`` is the explicit opt-in for a corporate or custom CA
    bundle.  An invalid explicit bundle is not silently replaced with another
    trust source.  No branch disables certificate verification.
    """

    configured_bundle = ca_bundle or os.environ.get("DARKFAC_CA_BUNDLE")
    if configured_bundle:
        try:
            return ssl.create_default_context(cafile=str(configured_bundle))
        except Exception as exc:
            raise TlsConfigurationError(
                TransportFailure(
                    TransportErrorKind.TLS_ERROR,
                    "configured CA bundle could not be loaded",
                )
            ) from exc

    try:
        import certifi

        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            # certifi can be unavailable or stale in a portable installation;
            # the OS trust store remains a verified alternative.
            pass
    except Exception:
        pass

    try:
        return ssl.create_default_context()
    except Exception as exc:
        raise TlsConfigurationError(
            TransportFailure(
                TransportErrorKind.TLS_ERROR,
                "no verified TLS context could be created",
            )
        ) from exc


class ResearchTransport:
    """Small urllib adapter with a fail-closed, structured error boundary."""

    def __init__(
        self,
        timeout_sec: float = 15,
        *,
        ca_bundle: str | Path | None = None,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.timeout_sec = timeout_sec
        self.ca_bundle = ca_bundle

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        """Perform one authenticated-by-TLS GET request.

        Only HTTPS URLs with a hostname are accepted.  Every external failure
        becomes a stable :class:`TransportError`; raw urllib exception text is
        deliberately not allowed through the boundary.
        """

        self._validate_url(url)
        request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")

        try:
            context = get_ssl_context(self.ca_bundle)
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_sec,
                context=context,
            ) as response:
                status_code = self._read_status_code(response)
                if status_code < 100 or status_code > 599:
                    raise _failure(
                        TransportErrorKind.INVALID_RESPONSE,
                        "remote service returned an invalid HTTP response",
                    )
                if status_code < 200 or status_code >= 300:
                    raise _failure(
                        TransportErrorKind.HTTP_STATUS,
                        "remote service returned an unsuccessful HTTP status",
                        status_code=status_code,
                        retryable=status_code == 429 or status_code >= 500,
                    )
                body = response.read()
                if not isinstance(body, bytes):
                    raise _failure(
                        TransportErrorKind.INVALID_RESPONSE,
                        "remote service returned a non-byte response body",
                    )
                if not body:
                    raise _failure(
                        TransportErrorKind.EMPTY_RESPONSE,
                        "remote service returned an empty response body",
                    )
                return TransportResponse(
                    status_code=status_code,
                    body=body,
                    headers=self._read_headers(response),
                )
        except TransportError:
            raise
        except urllib.error.HTTPError as exc:
            status_code = exc.code if isinstance(exc.code, int) else None
            raise _failure(
                TransportErrorKind.HTTP_STATUS,
                "remote service returned an unsuccessful HTTP status",
                status_code=status_code,
                retryable=status_code == 429 or (status_code is not None and status_code >= 500),
            ) from exc
        except (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError) as exc:
            raise _failure(
                TransportErrorKind.TLS_ERROR,
                "TLS certificate verification failed",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise _failure(
                TransportErrorKind.TIMEOUT,
                "remote service request timed out",
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            if self._is_timeout_reason(exc.reason):
                raise _failure(
                    TransportErrorKind.TIMEOUT,
                    "remote service request timed out",
                    retryable=True,
                ) from exc
            raise _failure(
                TransportErrorKind.TRANSPORT_ERROR,
                "remote service transport failed",
                retryable=True,
            ) from exc
        except (AttributeError, OSError, ValueError, TypeError) as exc:
            raise _failure(
                TransportErrorKind.TRANSPORT_ERROR,
                "remote service transport failed",
                retryable=True,
            ) from exc

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
        except (TypeError, ValueError) as exc:
            raise _failure(
                TransportErrorKind.TLS_ERROR,
                "research integrations require a valid HTTPS URL",
            ) from exc
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise _failure(
                TransportErrorKind.TLS_ERROR,
                "research integrations require a valid HTTPS URL",
            )

    @staticmethod
    def _read_status_code(response: Any) -> int:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode() if hasattr(response, "getcode") else None
        if isinstance(status, bool) or not isinstance(status, int):
            raise _failure(
                TransportErrorKind.INVALID_RESPONSE,
                "remote service returned an invalid HTTP response",
            )
        return status

    @staticmethod
    def _read_headers(response: Any) -> dict[str, str]:
        raw_headers = getattr(response, "headers", None)
        if raw_headers is None or not hasattr(raw_headers, "items"):
            return {}
        return {str(key): str(value) for key, value in raw_headers.items()}

    @staticmethod
    def _is_timeout_reason(reason: Any) -> bool:
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
        return isinstance(reason, OSError) and getattr(reason, "errno", None) in {
            60,
            110,
            10060,
        }


# Compatibility aliases for callers that prefer a query-oriented name.
ResearchQueryResult = ResearchSearchResult


__all__ = [
    "ResearchQueryResult",
    "ResearchSearchResult",
    "ResearchTransport",
    "SearchStatus",
    "TlsConfigurationError",
    "TransportError",
    "TransportErrorKind",
    "TransportFailure",
    "TransportResponse",
    "get_ssl_context",
]
