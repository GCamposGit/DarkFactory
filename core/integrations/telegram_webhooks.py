"""Idempotent (re)registration of the two Telegram bots' webhooks with the DarkHub.

Both bots used to point at the same URL, so the Hub could not tell which bot an update came
from. Each bot now has its own route:

- ops   (@darkfac_ops_bot): `<base>/api/webhooks/telegram/ops`   - demands, /linha, grill, buttons
- owner (@darkfac_bot):     `<base>/api/webhooks/telegram/owner` - alerts only

`register_telegram_webhooks` runs at Hub startup: for each bot it reads `getWebhookInfo` and calls
`setWebhook` (with the existing `TELEGRAM_WEBHOOK_SECRET` as `secret_token`) only when the URL
differs, so restarts are no-ops. Tokens are only read from the environment the Hub already has and
are never logged: log lines carry only the role and a status word (error text is not logged at all).

Registration only runs in production (`DARKHUB_ENV=production`) unless forced with
`DARKHUB_TELEGRAM_AUTO_WEBHOOK=true`; `DARKHUB_TELEGRAM_AUTO_WEBHOOK=false` disables it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_PUBLIC_URL = "https://darkhub.ggcampos.com"
ROUTE_PREFIX = "/api/webhooks/telegram"
ALLOWED_UPDATES = ["message", "callback_query"]
API_BASE = "https://api.telegram.org"

# `http(method, url, payload) -> parsed JSON` -- injectable so tests never touch the network.
HttpCall = Callable[[str, str, Optional[dict[str, Any]]], dict[str, Any]]


@dataclass(frozen=True)
class WebhookTarget:
    role: str
    token: str
    url: str


def auto_registration_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    raw = (env.get("DARKHUB_TELEGRAM_AUTO_WEBHOOK") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return (env.get("DARKHUB_ENV") or "").strip().lower() == "production"


def webhook_targets(env: Mapping[str, str] | None = None) -> list[WebhookTarget]:
    """One target per distinct bot token the Hub has (ops first)."""
    env = os.environ if env is None else env
    base = (env.get("DARKHUB_PUBLIC_URL") or DEFAULT_PUBLIC_URL).strip().rstrip("/")
    ops_token = (env.get("TELEGRAM_OPS_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    owner_token = (env.get("TELEGRAM_OWNER_BOT_TOKEN") or "").strip()
    targets: list[WebhookTarget] = []
    if ops_token:
        targets.append(WebhookTarget("ops", ops_token, f"{base}{ROUTE_PREFIX}/ops"))
    if owner_token and owner_token != ops_token:  # the same bot cannot have two webhooks
        targets.append(WebhookTarget("owner", owner_token, f"{base}{ROUTE_PREFIX}/owner"))
    return targets


def _default_http(method: str, url: str, payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Telegram API host
        return json.loads(response.read().decode("utf-8"))


def register_telegram_webhooks(
    env: Mapping[str, str] | None = None,
    *,
    http: HttpCall | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Point each bot at its own route. Returns `{role: "unchanged" | "updated" | "failed"}`.

    Never raises and never logs a token. Disabled (returns `{}`) outside production unless `force`.
    """
    env = os.environ if env is None else env
    if not force and not auto_registration_enabled(env):
        return {}
    call = http or _default_http
    secret = (env.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    results: dict[str, str] = {}
    for target in webhook_targets(env):
        api = f"{API_BASE}/bot{target.token}"
        try:
            info = call("GET", f"{api}/getWebhookInfo", None)
            current = ((info or {}).get("result") or {}).get("url") or ""
            if current == target.url:
                results[target.role] = "unchanged"
                continue
            payload: dict[str, Any] = {"url": target.url, "allowed_updates": ALLOWED_UPDATES}
            if secret:
                payload["secret_token"] = secret
            reply = call("POST", f"{api}/setWebhook", payload)
            if not (reply or {}).get("ok"):
                raise RuntimeError("setWebhook was not ok")
            results[target.role] = "updated"
        except Exception as exc:  # noqa: BLE001 - startup must never fail because of Telegram
            results[target.role] = "failed"
            # Never log the exception text: urllib/requests errors can embed the request URL, which
            # carries the bot token. The type (and HTTP status, if any) is enough to diagnose.
            status = getattr(exc, "code", None)
            logger.warning(
                "Telegram webhook registration failed for role=%s: %s%s",
                target.role,
                type(exc).__name__,
                f" (HTTP {status})" if isinstance(status, int) else "",
            )
            continue
        logger.info("Telegram webhook registered for role=%s route=%s", target.role, target.url.rsplit("/", 1)[-1])
    return results
