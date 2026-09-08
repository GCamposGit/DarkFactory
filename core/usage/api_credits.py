"""API Credits and Financial Balance Monitoring ($) for DarkFactory.

Decoupled, strictly typed domain module for tracking API expenditures in USD
and available account balances for active providers (OpenRouter, OpenAI)
and planned account placeholders.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return RFC 3339 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


class CreditAccountStatus(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    PLANNING = "planning"


class CreditOfficialLink(BaseModel):
    title: str
    url: str


class ProviderCreditCard(BaseModel):
    provider_id: str
    provider_name: str
    status: CreditAccountStatus
    is_connected: bool
    current_month_spend_usd: Optional[float] = None
    available_credit_usd: Optional[float] = None
    currency: str = "USD"
    official_links: List[CreditOfficialLink] = Field(default_factory=list)
    checked_at: str = Field(default_factory=utc_now_iso)
    notes: Optional[str] = None


class ApiCreditsReport(BaseModel):
    checked_at: str = Field(default_factory=utc_now_iso)
    total_month_spend_usd: float = 0.0
    total_available_credit_usd: Optional[float] = None
    accounts: List[ProviderCreditCard] = Field(default_factory=list)


class CreditAccountUpdateRequest(BaseModel):
    current_month_spend_usd: Optional[float] = None
    available_credit_usd: Optional[float] = None
    notes: Optional[str] = None


class ApiCreditsMonitor:
    """Inspects API balances and monthly spends across active and planned AI accounts."""

    CACHE_TTL_SECONDS = 60.0

    def __init__(self, snapshot_dir: Optional[Path] = None) -> None:
        if snapshot_dir is None:
            self.snapshot_dir = Path("c:/dev/DarkFac/.factory/usage/credits")
        else:
            self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._cached_report: Optional[ApiCreditsReport] = None
        self._last_cached_at: float = 0.0

    def save_snapshot(self, provider_id: str, payload: Dict[str, Any]) -> None:
        """Persists or updates an offline credit snapshot JSON file."""
        file_target = self.snapshot_dir / f"{provider_id}.json"
        existing = self._read_snapshot(provider_id) or {}
        existing.update(payload)
        file_target.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        self._cached_report = None

    def update_account(self, provider_id: str, update: CreditAccountUpdateRequest) -> ProviderCreditCard:
        """Updates and persists manual or synced values for a credit account."""
        update_data = update.model_dump(exclude_unset=True)
        self.save_snapshot(provider_id, update_data)
        if provider_id == "openai":
            return self.inspect_openai()
        elif provider_id == "openrouter":
            return self.inspect_openrouter()
        report = self.generate_report(force=True)
        for acc in report.accounts:
            if acc.provider_id == provider_id:
                return acc
        return self.inspect_openai()

    def get_openrouter_key(self) -> Optional[str]:
        """Recovers OpenRouter API key from environment or Windows registry."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key and sys.platform.startswith("win"):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                    key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
            except Exception as exc:
                logger.debug("Could not read OPENROUTER_API_KEY from registry: %s", exc)
        return key.strip() if (key and key.strip()) else None

    def get_openai_key(self) -> Optional[str]:
        """Recovers OpenAI API key from environment or Windows registry."""
        key = os.environ.get("OPENAI_API_KEY")
        if not key and sys.platform.startswith("win"):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                    key, _ = winreg.QueryValueEx(k, "OPENAI_API_KEY")
            except Exception as exc:
                logger.debug("Could not read OPENAI_API_KEY from registry: %s", exc)
        return key.strip() if (key and key.strip()) else None

    def _read_snapshot(self, provider_id: str) -> Optional[Dict[str, Any]]:
        """Safely reads offline snapshot JSON from env or filesystem."""
        env_var = f"DARKFAC_{provider_id.upper()}_CREDITS_JSON"
        configured = os.environ.get(env_var)
        if configured:
            conf_path = Path(configured).expanduser()
            if conf_path.is_file():
                try:
                    return json.loads(conf_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Failed to parse credits snapshot at %s: %s", conf_path, exc)
            else:
                try:
                    parsed = json.loads(configured)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    logger.warning("%s contains invalid JSON data", env_var)

        file_candidate = self.snapshot_dir / f"{provider_id}.json"
        if file_candidate.is_file():
            try:
                return json.loads(file_candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read snapshot file %s: %s", file_candidate, exc)
        return None

    def inspect_openrouter(self) -> ProviderCreditCard:
        """Inspects live OpenRouter balance and monthly spend, with fallback to snapshot."""
        official_links = [
            CreditOfficialLink(title="OpenRouter Activity", url="https://openrouter.ai/activity"),
            CreditOfficialLink(title="OpenRouter Credits", url="https://openrouter.ai/settings/credits"),
        ]

        key = self.get_openrouter_key()
        if not key:
            # Check snapshot before declaring disconnected
            snapshot = self._read_snapshot("openrouter")
            if snapshot:
                return ProviderCreditCard(
                    provider_id="openrouter",
                    provider_name="OpenRouter",
                    status=CreditAccountStatus(snapshot.get("status", "active")),
                    is_connected=True,
                    current_month_spend_usd=snapshot.get("current_month_spend_usd"),
                    available_credit_usd=snapshot.get("available_credit_usd"),
                    official_links=official_links,
                    notes=snapshot.get("notes", "Valores obtidos de snapshot offline"),
                )
            return ProviderCreditCard(
                provider_id="openrouter",
                provider_name="OpenRouter",
                status=CreditAccountStatus.DISCONNECTED,
                is_connected=False,
                current_month_spend_usd=None,
                available_credit_usd=None,
                official_links=official_links,
                notes="Chave OPENROUTER_API_KEY não configurada no sistema.",
            )

        spend_usd: Optional[float] = None
        available_credit_usd: Optional[float] = None
        status = CreditAccountStatus.ACTIVE
        notes: Optional[str] = None

        # 1. Fetch available credits from /api/v1/credits
        try:
            req_credits = urllib.request.Request(
                "https://openrouter.ai/api/v1/credits",
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://github.com/DarkFac",
                },
            )
            with urllib.request.urlopen(req_credits, timeout=6.0) as resp:
                credits_data = json.loads(resp.read().decode("utf-8")).get("data", {})
                total_credits = float(credits_data.get("total_credits", 0.0))
                total_usage = float(credits_data.get("total_usage", 0.0))
                available_credit_usd = round(max(0.0, total_credits - total_usage), 2)
        except Exception as exc:
            logger.warning("Failed to query OpenRouter credits: %s", exc)
            status = CreditAccountStatus.DEGRADED
            notes = f"Aviso na consulta de saldo: {exc}"

        # 2. Fetch usage from /api/v1/auth/key
        try:
            req_auth = urllib.request.Request(
                "https://openrouter.ai/api/v1/auth/key",
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://github.com/DarkFac",
                },
            )
            with urllib.request.urlopen(req_auth, timeout=6.0) as resp:
                auth_data = json.loads(resp.read().decode("utf-8")).get("data", {})
                spend_usd = round(float(auth_data.get("usage", 0.0)), 2)
                limit_usd = auth_data.get("limit")
                if available_credit_usd is None and limit_usd is not None:
                    available_credit_usd = round(max(0.0, float(limit_usd) - spend_usd), 2)
                if status == CreditAccountStatus.DEGRADED and available_credit_usd is not None:
                    status = CreditAccountStatus.ACTIVE
        except Exception as exc:
            logger.warning("Failed to query OpenRouter auth/key: %s", exc)
            if status != CreditAccountStatus.ACTIVE:
                status = CreditAccountStatus.DEGRADED
                notes = f"Falha na consulta OpenRouter: {exc}"

        # Fallback to snapshot if complete network failure occurred
        if available_credit_usd is None and spend_usd is None:
            snapshot = self._read_snapshot("openrouter")
            if snapshot:
                spend_usd = snapshot.get("current_month_spend_usd")
                available_credit_usd = snapshot.get("available_credit_usd")
                notes = "Valores de fallback obtidos de snapshot offline."

        return ProviderCreditCard(
            provider_id="openrouter",
            provider_name="OpenRouter",
            status=status,
            is_connected=True,
            current_month_spend_usd=spend_usd,
            available_credit_usd=available_credit_usd,
            official_links=official_links,
            notes=notes,
        )

    def inspect_openai(self) -> ProviderCreditCard:
        """Inspects live OpenAI API account, connectivity, and snapshot billing."""
        official_links = [
            CreditOfficialLink(title="OpenAI Platform Home", url="https://platform.openai.com/home"),
        ]

        key = self.get_openai_key()
        snapshot = self._read_snapshot("openai")

        if not key and not snapshot:
            return ProviderCreditCard(
                provider_id="openai",
                provider_name="OpenAI API",
                status=CreditAccountStatus.DISCONNECTED,
                is_connected=False,
                current_month_spend_usd=None,
                available_credit_usd=None,
                official_links=official_links,
                notes="Chave OPENAI_API_KEY não configurada no sistema.",
            )

        spend_usd: Optional[float] = snapshot.get("current_month_spend_usd") if snapshot else None
        available_usd: Optional[float] = snapshot.get("available_credit_usd") if snapshot else None
        notes: Optional[str] = snapshot.get("notes") if snapshot else None
        status = CreditAccountStatus.ACTIVE

        if key:
            # Probe models endpoint to verify key is live and valid
            try:
                req_models = urllib.request.Request(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with urllib.request.urlopen(req_models, timeout=5.0) as resp:
                    if resp.status == 200:
                        if not notes:
                            notes = "Conectado via API Key. Acesse o dashboard oficial para faturamento detalhado."
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    status = CreditAccountStatus.DEGRADED
                    notes = "Chave OPENAI_API_KEY inválida ou não autorizada."
                else:
                    logger.debug("OpenAI probe returned code %s", exc.code)
            except Exception as exc:
                logger.debug("OpenAI network probe skipped/failed: %s", exc)

        return ProviderCreditCard(
            provider_id="openai",
            provider_name="OpenAI API",
            status=status,
            is_connected=(status == CreditAccountStatus.ACTIVE),
            current_month_spend_usd=spend_usd,
            available_credit_usd=available_usd,
            official_links=official_links,
            notes=notes,
        )

    def inspect_planned_providers(self) -> List[ProviderCreditCard]:
        """Generates placeholders for all planned providers with official portal links."""
        planned_specs = [
            (
                "anthropic",
                "Anthropic / Claude",
                [
                    CreditOfficialLink(title="Claude Console Plans", url="https://console.anthropic.com/settings/plans"),
                    CreditOfficialLink(title="Claude Billing", url="https://console.anthropic.com/settings/billing"),
                ],
            ),
            (
                "google",
                "Google AI Studio / Gemini",
                [
                    CreditOfficialLink(title="Google AI Studio", url="https://aistudio.google.com/"),
                    CreditOfficialLink(title="Google Cloud Billing", url="https://console.cloud.google.com/billing"),
                ],
            ),
            (
                "deepseek",
                "DeepSeek",
                [
                    CreditOfficialLink(title="DeepSeek Usage", url="https://platform.deepseek.com/usage"),
                ],
            ),
            (
                "xai",
                "xAI / Grok",
                [
                    CreditOfficialLink(title="xAI Console", url="https://console.x.ai/"),
                ],
            ),
            (
                "moonshot",
                "Moonshot / Kimi",
                [
                    CreditOfficialLink(title="Moonshot Console", url="https://platform.moonshot.cn/console/info"),
                ],
            ),
            (
                "qwen",
                "Alibaba Qwen / Bailian",
                [
                    CreditOfficialLink(title="Bailian Console", url="https://bailian.console.aliyun.com/"),
                ],
            ),
            (
                "siliconflow",
                "SiliconFlow",
                [
                    CreditOfficialLink(title="SiliconFlow Account", url="https://cloud.siliconflow.cn/account/ak"),
                ],
            ),
            (
                "minimax",
                "MiniMax",
                [
                    CreditOfficialLink(title="MiniMax Platform", url="https://platform.minimax.io/"),
                ],
            ),
            (
                "zhipu",
                "Zhipu / GLM",
                [
                    CreditOfficialLink(title="Zhipu Console", url="https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys"),
                ],
            ),
        ]

        cards: List[ProviderCreditCard] = []
        for pid, name, links in planned_specs:
            cards.append(
                ProviderCreditCard(
                    provider_id=pid,
                    provider_name=name,
                    status=CreditAccountStatus.PLANNING,
                    is_connected=False,
                    current_month_spend_usd=None,
                    available_credit_usd=None,
                    official_links=links,
                    notes="Conta planejada para expansão da DarkFactory.",
                )
            )
        return cards

    def generate_report(self, force: bool = False) -> ApiCreditsReport:
        """Assembles aggregated report across active and planned accounts."""
        now = time.time()
        if not force and self._cached_report and (now - self._last_cached_at < self.CACHE_TTL_SECONDS):
            return self._cached_report

        accounts: List[ProviderCreditCard] = []

        # Active providers
        accounts.append(self.inspect_openrouter())
        accounts.append(self.inspect_openai())

        # Planned placeholders
        accounts.extend(self.inspect_planned_providers())

        # Financial totals
        total_spend: float = 0.0
        total_available: Optional[float] = None

        for acc in accounts:
            if acc.current_month_spend_usd is not None:
                total_spend += acc.current_month_spend_usd
            if acc.available_credit_usd is not None:
                if total_available is None:
                    total_available = 0.0
                total_available += acc.available_credit_usd

        total_spend = round(total_spend, 2)
        if total_available is not None:
            total_available = round(total_available, 2)

        report = ApiCreditsReport(
            checked_at=utc_now_iso(),
            total_month_spend_usd=total_spend,
            total_available_credit_usd=total_available,
            accounts=accounts,
        )

        self._cached_report = report
        self._last_cached_at = now
        return report

    def refresh_report(self) -> ApiCreditsReport:
        """Forces probe refresh and updates cache."""
        return self.generate_report(force=True)
