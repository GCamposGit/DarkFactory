"""Real qualification executor for model candidates via OpenRouter (USR-341).

Owner decisions (Grill, 22/10/2026), kept in ``.factory/config/qualification_spend.json``: US$ 1.00/day total cap,
US$ 0.50 per candidate, OpenRouter only (key in ``OPENROUTER_API_KEY``; absent => the qualifier does not run, no
fatal error), a candidate without a known price is never called and goes to the Grill instead.

Every call is gated by a cost estimate made BEFORE the request; the real cost comes from the response ``usage``.
Daily spend is persisted in ``.factory/models/spend/openrouter_spend.json`` (gitignored). HTTP is injectable.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.models import qualification
from core.models.discovery import OPENROUTER_MODELS_URL, _per_mtok
from core.models.registry import REPO_ROOT, parse_registry

logger = logging.getLogger(__name__)

CONFIG_PATH = REPO_ROOT / ".factory" / "config" / "qualification_spend.json"
SPEND_DIR = REPO_ROOT / ".factory" / "models" / "spend"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_UA = {"User-Agent": "DarkFactory/1.0 (Autonomous Software Engine)"}

HttpPost = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]
FetchCatalog = Callable[[], list[dict[str, Any]]]


class SpendRefused(Exception):
    """A call was refused before being made because it would break a cap."""


@dataclass(frozen=True)
class SpendPolicy:
    daily_cap_usd: float = 1.0
    per_candidate_cap_usd: float = 0.5
    max_output_tokens: int = 512
    api_key_env: str = "OPENROUTER_API_KEY"


def load_policy(path: Optional[Path] = None) -> SpendPolicy:
    try:
        raw = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SpendPolicy()
    return SpendPolicy(
        float(raw.get("daily_cap_usd", 1.0)), float(raw.get("per_candidate_cap_usd", 0.5)),
        int(raw.get("max_output_tokens", 512)), str(raw.get("api_key_env", "OPENROUTER_API_KEY")),
    )


class SpendLedger:
    """Per-UTC-day spend, persisted as JSON (resets when the date changes)."""

    def __init__(self, day: str, spend_dir: Optional[Path] = None) -> None:
        self.day = day
        self.path = (spend_dir or SPEND_DIR) / "openrouter_spend.json"
        self.total = 0.0
        self.by_model: dict[str, float] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("date") == day:
                self.total = float(raw.get("total_usd", 0.0))
                self.by_model = {str(k): float(v) for k, v in (raw.get("by_model") or {}).items()}
        except (OSError, ValueError):
            pass

    def add(self, model_id: str, usd: float) -> None:
        self.total += usd
        self.by_model[model_id] = self.by_model.get(model_id, 0.0) + usd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": self.day, "total_usd": self.total, "by_model": self.by_model}, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )


def _default_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={**_UA, **headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read().decode("utf-8"))


def _default_catalog() -> list[dict[str, Any]]:
    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={**_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URL
        payload = json.loads(resp.read().decode("utf-8"))
    return [i for i in payload.get("data", []) if isinstance(i, dict)] if isinstance(payload, dict) else []


class OpenRouterExecutor:
    """``ModelExecutor`` calling OpenRouter with a pre-call cost estimate and a persisted spend ledger."""

    def __init__(self, *, api_key: str, ledger: SpendLedger, policy: SpendPolicy,
                 prices: dict[str, tuple[float, float]], http_post: Optional[HttpPost] = None) -> None:
        self.api_key, self.ledger, self.policy, self.prices = api_key, ledger, policy, prices
        self.http_post = http_post or _default_post
        self.refused: Optional[str] = None

    def estimate(self, model_id: str, prompt: str, tools: Optional[list[dict[str, Any]]]) -> float:
        p_in, p_out = self.prices[model_id]
        in_tokens = (len(prompt) + len(json.dumps(tools or []))) // 3 + 16  # conservative: ~3 chars/token
        return (in_tokens * p_in + self.policy.max_output_tokens * p_out) / 1_000_000

    def __call__(self, model_id: str, prompt: str, tools: Optional[list[dict[str, Any]]] = None) -> qualification.ExecResult:
        est = self.estimate(model_id, prompt, tools)
        if self.ledger.total + est > self.policy.daily_cap_usd + 1e-12:
            self.refused = "daily_cap"
            raise SpendRefused(f"daily cap: spent {self.ledger.total:.6f} + est {est:.6f} > {self.policy.daily_cap_usd}")
        if self.ledger.by_model.get(model_id, 0.0) + est > self.policy.per_candidate_cap_usd + 1e-12:
            self.refused = "candidate_cap"
            raise SpendRefused(f"candidate cap: {model_id} + est {est:.6f} > {self.policy.per_candidate_cap_usd}")
        body: dict[str, Any] = {"model": model_id, "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": self.policy.max_output_tokens, "temperature": 0}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        data = self.http_post(CHAT_URL, {"Authorization": f"Bearer {self.api_key}"}, body)
        usage = data.get("usage") or {}
        p_in, p_out = self.prices[model_id]
        in_t, out_t = int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)
        cost = usage.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) else (in_t * p_in + out_t * p_out) / 1_000_000
        self.ledger.add(model_id, cost)
        msg = ((data.get("choices") or [{}])[0].get("message")) or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"name": fn.get("name"), "arguments": args})
        return qualification.ExecResult(str(msg.get("content") or ""), tuple(calls), in_t, out_t, cost)


def fill_prices(reg_path: Path, ids: list[str], catalog: list[dict[str, Any]]) -> None:
    """Fill missing candidate prices from the OpenRouter catalog (never overwrites a known price)."""
    by_id = {str(i.get("id")): i for i in catalog}
    raw = json.loads(reg_path.read_text(encoding="utf-8"))
    changed = False
    for model in raw["models"]:
        if model["id"] not in ids or model.get("status") != "candidate":
            continue
        pricing = (by_id.get(model["id"]) or {}).get("pricing") or {}
        for key, src in (("price_input_per_mtok", "prompt"), ("price_output_per_mtok", "completion")):
            val = _per_mtok(pricing.get(src))
            if model.get(key) is None and val is not None:
                model[key] = val
                changed = True
    if changed:
        parse_registry(raw)
        tmp = reg_path.with_suffix(reg_path.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, reg_path)


def make_qualify_func(
    *, now: datetime, env: Optional[dict[str, str]] = None, policy: Optional[SpendPolicy] = None,
    http_post: Optional[HttpPost] = None, fetch_catalog: Optional[FetchCatalog] = None,
    spend_dir: Optional[Path] = None, evidence_dir: Optional[Path] = None,
) -> Optional[Callable[[Path, list[str]], None]]:
    """Build the default ``qualify_func``; ``None`` (not an error) when the API key is absent."""
    pol = policy or load_policy()
    key = (env if env is not None else os.environ).get(pol.api_key_env, "").strip()
    if not key:
        logger.info("%s not set: autonomous qualification disabled", pol.api_key_env)
        return None

    def qualify(reg_path: Path, ids: list[str]) -> None:
        try:
            fill_prices(reg_path, ids, (fetch_catalog or _default_catalog)())
        except Exception as exc:  # noqa: BLE001 - price fill is best effort; unknown price => Grill
            logger.warning("OpenRouter catalog unavailable: %s", exc)
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
        ledger = SpendLedger(now.date().isoformat(), spend_dir)
        for model in raw["models"]:
            mid = model["id"]
            if mid not in ids or model.get("status") != "candidate":
                continue
            p_in, p_out = model.get("price_input_per_mtok"), model.get("price_output_per_mtok")
            if p_in is None or p_out is None:
                logger.info("%s has no known price: not called, goes to the Grill", mid)
                continue
            executor = OpenRouterExecutor(api_key=key, ledger=ledger, policy=pol,
                                          prices={mid: (float(p_in), float(p_out))}, http_post=http_post)
            result = qualification.run_battery(
                mid, executor, now=now, budget=qualification.Budget(max_usd=pol.per_candidate_cap_usd))
            if executor.refused:  # cap refusal is not a verdict on the model: leave it unqualified for a later day
                logger.info("%s not (fully) qualified: %s", mid, executor.refused)
                continue
            path = qualification.persist_evidence(result, evidence_dir)
            qualification.record_in_registry(result, path, reg_path)

    return qualify
