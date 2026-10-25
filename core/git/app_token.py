"""GitHub App installation token for autonomous push/merge (USR-343 S2).

Signs a short-lived RS256 JWT with ``cryptography`` and exchanges it for an installation token scoped
to ``contents:write`` and ``pull_requests:write``. The token is cached in-process until 5 minutes
before expiry. Returns ``None`` when the App is not configured, so callers keep their old behavior.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Mapping, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
REFRESH_MARGIN_S = 300.0
PERMISSIONS = {"contents": "write", "pull_requests": "write"}
_ENV_KEYS = ("GH_APP_ID", "GH_APP_INSTALLATION_ID", "GH_APP_PRIVATE_KEY")

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(app_id: str, private_key_pem: str, now: float) -> str:
    """RS256 JWT valid for 9 minutes (GitHub caps at 10)."""
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(app_id)}).encode())
    key = serialization.load_pem_private_key(private_key_pem.replace("\\n", "\n").encode(), password=None)
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[call-arg]
    return f"{header}.{payload}.{_b64(signature)}"


def is_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    source = os.environ if env is None else env
    return all(source.get(k) for k in _ENV_KEYS)


def get_app_token(
    env: Optional[Mapping[str, str]] = None,
    *,
    client: Optional[httpx.Client] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """Return a cached installation token, or ``None`` when unconfigured or the exchange fails."""
    source = os.environ if env is None else env
    if not is_configured(source):
        return None
    app_id, inst = source["GH_APP_ID"], source["GH_APP_INSTALLATION_ID"]
    ts = time.time() if now is None else now
    with _lock:
        hit = _cache.get(inst)
        if hit and hit[1] - REFRESH_MARGIN_S > ts:
            return hit[0]
        try:
            jwt_token = make_jwt(app_id, source["GH_APP_PRIVATE_KEY"], ts)
            http = client or httpx.Client(timeout=20.0)
            try:
                resp = http.post(
                    f"{API_ROOT}/app/installations/{inst}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {jwt_token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "darkfac-factory",
                    },
                    json={"permissions": PERMISSIONS},
                )
            finally:
                if client is None:
                    http.close()
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
            token = str(body["token"])
            expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00")).timestamp()
        except Exception as exc:  # fall back to the legacy auth path; never log secrets
            logger.warning("GitHub App token unavailable (%s); using ambient gh auth", type(exc).__name__)
            return None
        _cache[inst] = (token, expires)
        return token


def clear_cache() -> None:
    with _lock:
        _cache.clear()
