"""Owner authentication primitives for the DarkHub HTTP layer.

The public Hub (behind Traefik) must never hand out the owner session to anonymous callers. Three
credentials are accepted for state-changing requests, none of which needs new configuration:

- local requests (socket peer on loopback, no proxy headers): the owner's own machine;
- the owner session token (``X-Hub-Session``), obtained remotely only through a one-time login link
  sent to the owner's Telegram chat (stateless HMAC code, 10 minute TTL, keyed by the session token);
- the machine telemetry key (``X-DarkFac-Telemetry-Key``) that workers already use for sync routes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Iterable, Mapping

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Routes that authenticate themselves (HMAC / Telegram secret) or start the login flow.
MUTATION_EXEMPT_PATHS = frozenset(
    {
        "/api/webhooks/github",
        "/api/webhooks/telegram",
        "/api/webhooks/telegram/ops",
        "/api/webhooks/telegram/owner",
        "/api/auth/telegram-link",
    }
)

# "testclient" is the peer name Starlette's TestClient uses; real sockets always carry an IP address.
_LOCAL_PEERS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_PROXY_HEADERS = (b"x-forwarded-for", b"x-real-ip", b"forwarded", b"cf-connecting-ip", b"x-forwarded-host")

LOGIN_CODE_TTL_SEC = 600


def is_local_request(client: Iterable[object] | None, headers: Mapping[bytes, bytes]) -> bool:
    """True only for a direct loopback connection; anything relayed by a proxy is remote."""
    if not client:
        return False
    host = str(next(iter(client), "")).strip().lower()
    if host not in _LOCAL_PEERS:
        return False
    return not any(headers.get(name) for name in _PROXY_HEADERS)


def telemetry_key_valid(provided: str | None) -> bool:
    configured = (os.environ.get("DARKFAC_TELEMETRY_KEY") or "").strip()
    if not configured or not provided:
        return False
    return hmac.compare_digest(provided.strip(), configured)


def is_mutation_exempt(path: str) -> bool:
    return (path.rstrip("/") or "/") in MUTATION_EXEMPT_PATHS


def _login_signature(session_token: str, expires_at: int) -> str:
    message = f"darkhub-login:{expires_at}".encode()
    return hmac.new(session_token.encode("utf-8"), message, hashlib.sha256).hexdigest()[:40]


def make_login_code(session_token: str, now: float | None = None) -> str:
    expires_at = int(now if now is not None else time.time()) + LOGIN_CODE_TTL_SEC
    return f"{expires_at}-{_login_signature(session_token, expires_at)}"


def verify_login_code(code: str | None, session_token: str, now: float | None = None) -> bool:
    if not code or "-" not in code:
        return False
    raw_expiry, signature = code.split("-", 1)
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if expires_at < current or expires_at - current > LOGIN_CODE_TTL_SEC:
        return False
    return hmac.compare_digest(signature, _login_signature(session_token, expires_at))
