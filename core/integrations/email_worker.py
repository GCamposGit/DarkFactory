"""Client for the darkfac-mailer Cloudflare Worker (OA-034).

The factory's only outbound email channel: a POST to ``DARKFAC_MAILER_URL`` with a
``Bearer DARKFAC_MAILER_TOKEN`` header delivers a plain-text message to the grokbot
mailbox. The Worker's own contract (``infra/cloudflare/darkfac-mailer/worker.js``):
200 ``{"ok": true, ...}`` on success, 401 on a bad/missing token, 502
``{"ok": false, "error": ...}`` when the underlying Email Routing send fails.

Used by :mod:`core.line.grokbox_wake` (USR-310) to wake the grokbot when the
official gate's test worker is unreachable; never exposes the token in a
returned error, and never raises -- every failure mode (missing env vars, a
non-2xx response, a network error) comes back as a structured ``{"ok": False,
"error": ...}`` for the caller to log.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

ENV_MAILER_URL = "DARKFAC_MAILER_URL"
ENV_MAILER_TOKEN = "DARKFAC_MAILER_TOKEN"
DEFAULT_TIMEOUT_SEC = 15.0
#: Cloudflare answers the default ``Python-urllib/x.y`` agent with 403 "error code: 1010"
#: (browser integrity check) before the request ever reaches the Worker.
USER_AGENT = "DarkFac-mailer-client/1.0"


def _user_registry_env(name: str) -> str:
    """Windows User environment (HKCU\\Environment); empty elsewhere or when unset."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _kind = winreg.QueryValueEx(key, name)
        return str(raw).strip()
    except OSError:
        return ""


def _env(name: str) -> str:
    """Process env first, then the Windows User environment: a shell opened before the
    variable was set never inherits it (nor does a run_ticket launched from it)."""
    return os.environ.get(name, "").strip() or _user_registry_env(name)


def send_grokbot_email(subject: str, text: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """POST ``{subject, text}`` to the darkfac-mailer Worker. Returns ``{"ok": True}``
    only when the Worker itself answered 200 with ``{"ok": true}``; any other outcome
    (missing env vars, HTTP error, network error) returns ``{"ok": False, "error": ...}``.
    """

    url = _env(ENV_MAILER_URL)
    token = _env(ENV_MAILER_TOKEN)
    missing = [name for name, value in ((ENV_MAILER_URL, url), (ENV_MAILER_TOKEN, token)) if not value]
    if missing:
        return {"ok": False, "error": f"missing env var(s): {', '.join(missing)}"}

    payload = json.dumps({"subject": subject, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            if resp.status == 200 and isinstance(body, dict) and body.get("ok") is True:
                return {"ok": True}
            return {"ok": False, "error": f"unexpected response (status {resp.status})"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"network error: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001 - this client must never raise into its caller
        return {"ok": False, "error": str(exc)}
