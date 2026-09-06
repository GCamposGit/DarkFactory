"""Provider adapters for subscription and API quota monitoring.

# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar
# Audit Doc: .factory/research/20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar/ledger.json
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.usage.models import (
    AccountConnectionStatus,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    provider_name: str
    family: ProviderFamily
    dashboard_url: str
    env_keys: tuple[str, ...] = ()


DEFAULT_PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "OpenAI / Codex", ProviderFamily.FRONTIER, "https://chatgpt.com/codex/settings/usage", ("OPENAI_API_KEY",)),
    ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://console.x.ai/", ("XAI_API_KEY", "XAI_MANAGEMENT_API_KEY")),
    ProviderSpec("google", "Google / Gemini", ProviderFamily.FRONTIER, "https://ai.dev/usage", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://console.anthropic.com/settings/usage", ("ANTHROPIC_API_KEY",)),
    ProviderSpec("openrouter", "OpenRouter", ProviderFamily.GATEWAY, "https://openrouter.ai/activity", ("OPENROUTER_API_KEY",)),
    ProviderSpec("deepseek", "DeepSeek", ProviderFamily.CHINESE, "https://platform.deepseek.com/usage", ("DEEPSEEK_API_KEY",)),
    ProviderSpec("siliconflow", "SiliconFlow", ProviderFamily.GATEWAY, "https://cloud.siliconflow.cn/account/ak", ("SILICONFLOW_API_KEY",)),
    ProviderSpec("qwen", "Alibaba Qwen", ProviderFamily.CHINESE, "https://bailian.console.aliyun.com/", ("DASHSCOPE_API_KEY",)),
    ProviderSpec("moonshot", "Moonshot / Kimi", ProviderFamily.CHINESE, "https://platform.moonshot.cn/console/info", ("MOONSHOT_API_KEY",)),
    ProviderSpec("zhipu", "Zhipu / GLM", ProviderFamily.CHINESE, "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys", ("ZHIPU_API_KEY",)),
    ProviderSpec("minimax", "MiniMax", ProviderFamily.CHINESE, "https://platform.minimax.io/", ("MINIMAX_API_KEY",)),
    ProviderSpec("ollama", "Ollama Local", ProviderFamily.LOCAL, "http://localhost:11434"),
)


def _clamp_percent(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(max(0.0, min(100.0, float(value))), 2)
    except (TypeError, ValueError):
        return None


def _timestamp_to_iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return str(value)


def _duration_label(minutes: Optional[int]) -> str:
    if not minutes:
        return "Quota"
    if minutes % 10080 == 0:
        return f"{minutes // 10080} semana"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} dia"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes} min"


class AccountUsageAdapter(ABC):
    def __init__(self, spec: ProviderSpec, snapshot_dir: Path) -> None:
        self.spec = spec
        self.snapshot_dir = Path(snapshot_dir)

    @abstractmethod
    def inspect(self) -> ProviderAccountUsage:
        """Read current status without ever returning credential material."""

    def disconnected(self, message: str, adapter: str) -> ProviderAccountUsage:
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.DISCONNECTED,
            adapter=adapter,
            message=message,
            dashboard_url=self.spec.dashboard_url,
        )

    def degraded(self, message: str, adapter: str) -> ProviderAccountUsage:
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.DEGRADED,
            adapter=adapter,
            message=message,
            dashboard_url=self.spec.dashboard_url,
        )

    def connected_without_quota(self, message: str, adapter: str) -> ProviderAccountUsage:
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.CONNECTED,
            adapter=adapter,
            quota_supported=False,
            message=message,
            dashboard_url=self.spec.dashboard_url,
        )
    def _snapshot_payload(self) -> Optional[Dict[str, Any]]:
        env_name = f"DARKFAC_{self.spec.provider_id.upper()}_USAGE_JSON"
        configured = os.environ.get(env_name)
        candidates: List[Path] = []
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.exists():
                candidates.append(configured_path)
            else:
                try:
                    payload = json.loads(configured)
                    return payload if isinstance(payload, dict) else None
                except json.JSONDecodeError:
                    logger.warning("%s is neither valid JSON nor an existing path", env_name)
        candidates.append(self.snapshot_dir / f"{self.spec.provider_id}.json")
        for candidate in candidates:
            try:
                if candidate.is_file():
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        return payload
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Cannot read quota snapshot %s: %s", candidate, exc)
        return None

    def _from_snapshot(self, payload: Dict[str, Any], adapter: str = "json_snapshot") -> ProviderAccountUsage:
        raw_windows = payload.get("windows", [])
        windows: List[QuotaWindow] = []
        if isinstance(raw_windows, list):
            for index, raw in enumerate(raw_windows):
                if not isinstance(raw, dict):
                    continue
                used = _clamp_percent(raw.get("used_percent", raw.get("usedPercent")))
                remaining = _clamp_percent(raw.get("remaining_percent", raw.get("remainingPercent")))
                if remaining is None and used is not None:
                    remaining = round(100.0 - used, 2)
                if used is None and remaining is not None:
                    used = round(100.0 - remaining, 2)
                duration = raw.get("window_duration_minutes", raw.get("windowDurationMins"))
                try:
                    duration_minutes = int(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration_minutes = None
                windows.append(
                    QuotaWindow(
                        quota_id=str(raw.get("quota_id", raw.get("id", index))),
                        label=str(raw.get("label") or _duration_label(duration_minutes)),
                        used_percent=used,
                        remaining_percent=remaining,
                        window_duration_minutes=duration_minutes,
                        resets_at=_timestamp_to_iso(raw.get("resets_at", raw.get("resetsAt"))),
                        metric=str(raw.get("metric", "subscription")),
                    )
                )
        try:
            status = AccountConnectionStatus(str(payload.get("status", "connected")).lower())
        except ValueError:
            status = AccountConnectionStatus.UNKNOWN
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=status,
            adapter=adapter,
            plan=str(payload["plan"]) if payload.get("plan") else None,
            account_label=str(payload["account_label"]) if payload.get("account_label") else None,
            quota_supported=bool(windows),
            windows=windows,
            message=str(payload.get("message") or ("Snapshot de quota carregado." if windows else "Conta detectada; quota não informada.")),
            dashboard_url=str(payload.get("dashboard_url") or self.spec.dashboard_url),
            checked_at=str(payload.get("checked_at") or datetime.now(timezone.utc).isoformat()),
        )


class CodexAccountAdapter(AccountUsageAdapter):
    """Read ChatGPT/Codex rolling buckets from the local official app-server."""

    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)
        executable = shutil.which("codex")
        if not executable:
            if any(os.environ.get(key) for key in self.spec.env_keys):
                return self.connected_without_quota(
                    "OpenAI API configurada; a credencial não expõe a quota da assinatura ChatGPT.",
                    "openai_api_key",
                )
            return self.disconnected("Codex não instalado ou fora do PATH.", "codex_app_server")
        try:
            return self._from_codex_response(self._read_rate_limits(executable))
        except Exception as exc:
            logger.info("Codex quota probe unavailable: %s", exc)
            if self._codex_doctor(executable):
                return ProviderAccountUsage(
                    provider_id=self.spec.provider_id,
                    provider_name=self.spec.provider_name,
                    family=self.spec.family,
                    status=AccountConnectionStatus.CONNECTED,
                    adapter="codex_doctor",
                    quota_supported=False,
                    message="Codex autenticado; o app-server não devolveu os buckets nesta leitura.",
                    dashboard_url=self.spec.dashboard_url,
                )
            if any(os.environ.get(key) for key in self.spec.env_keys):
                return self.connected_without_quota(
                    "OpenAI API configurada; a quota da assinatura ChatGPT não ficou disponível.",
                    "openai_api_key",
                )
            return self.disconnected("Codex não autenticado ou app-server indisponível.", "codex_app_server")

    @staticmethod
    def _codex_doctor(executable: str) -> bool:
        try:
            result = subprocess.run(
                [executable, "doctor", "--json"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=8, check=False,
            )
            payload = json.loads(result.stdout)
            auth = payload.get("checks", {}).get("auth.credentials", {})
            return auth.get("status") == "ok" and auth.get("summary") == "auth is configured"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return False

    @staticmethod
    def _read_rate_limits(executable: str, timeout_sec: float = 10.0) -> Dict[str, Any]:
        process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        messages: queue.Queue[Dict[str, Any]] = queue.Queue()

        def read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    messages.put(payload)

        threading.Thread(target=read_stdout, daemon=True).start()
        deadline = time.monotonic() + timeout_sec
        try:
            if process.stdin is None:
                raise RuntimeError("Codex app-server stdin unavailable")

            def send(payload: Dict[str, Any]) -> None:
                process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                process.stdin.flush()

            def receive(response_id: int) -> Dict[str, Any]:
                while time.monotonic() < deadline:
                    try:
                        message = messages.get(timeout=max(0.05, deadline - time.monotonic()))
                    except queue.Empty as exc:
                        raise TimeoutError(f"Codex response {response_id} timed out") from exc
                    if message.get("id") == response_id:
                        if "error" in message:
                            raise RuntimeError(str(message["error"]))
                        result = message.get("result")
                        if isinstance(result, dict):
                            return result
                        raise RuntimeError(f"Codex response {response_id} omitted result")
                raise TimeoutError(f"Codex response {response_id} timed out")

            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "darkhub", "version": "1.0.0"}}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            return receive(2)
        finally:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _from_codex_response(self, payload: Dict[str, Any]) -> ProviderAccountUsage:
        buckets = payload.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            single = payload.get("rateLimits")
            buckets = {"codex": single} if isinstance(single, dict) else {}
        if not buckets:
            raise RuntimeError("Codex returned no rate-limit buckets")
        windows: List[QuotaWindow] = []
        plan: Optional[str] = None
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            plan = plan or (str(bucket["planType"]) if bucket.get("planType") else None)
            bucket_name = str(bucket.get("limitName") or bucket_id)
            for slot in ("primary", "secondary"):
                raw = bucket.get(slot)
                if not isinstance(raw, dict):
                    continue
                used = _clamp_percent(raw.get("usedPercent"))
                duration_raw = raw.get("windowDurationMins")
                duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
                windows.append(QuotaWindow(
                    quota_id=f"{bucket_id}:{slot}", label=f"{bucket_name} · {_duration_label(duration)}",
                    used_percent=used, remaining_percent=round(100.0 - used, 2) if used is not None else None,
                    window_duration_minutes=duration, resets_at=_timestamp_to_iso(raw.get("resetsAt")),
                ))
        limited = any(window.used_percent is not None and window.used_percent >= 100.0 for window in windows)
        account_id = payload.get("accountId")
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
            family=self.spec.family, status=AccountConnectionStatus.LIMITED if limited else AccountConnectionStatus.CONNECTED,
            adapter="codex_app_server", plan=plan,
            account_label=f"…{str(account_id)[-6:]}" if account_id else None,
            quota_supported=bool(windows), windows=windows,
            message="Limites lidos da sessão local do Codex." if windows else "Codex conectado; sem buckets ativos.",
            dashboard_url=self.spec.dashboard_url,
        )


class GrokAccountAdapter(AccountUsageAdapter):
    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)
        executable = shutil.which("grok")
        if not executable:
            if any(os.environ.get(key) for key in self.spec.env_keys):
                return self.connected_without_quota(
                    "xAI API configurada; a credencial não expõe a quota do plano Grok.",
                    "xai_api_key",
                )
            return self.disconnected("Grok Build não instalado ou fora do PATH.", "grok_cli")
        try:
            result = subprocess.run(
                [executable, "models"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=12, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self.degraded(f"Grok instalado, mas o probe falhou: {type(exc).__name__}.", "grok_cli")
        if result.returncode != 0:
            if any(os.environ.get(key) for key in self.spec.env_keys):
                return self.connected_without_quota(
                    "xAI API configurada; a sessão Grok do CLI não foi validada.",
                    "xai_api_key",
                )
            return self.disconnected("Grok instalado, porém sem sessão autenticada verificável.", "grok_cli")
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
            family=self.spec.family, status=AccountConnectionStatus.CONNECTED, adapter="grok_cli",
            quota_supported=False,
            message="Sessão Grok validada; o plano não expõe percentual por CLI. Use snapshot ou Management API para billing.",
            dashboard_url=self.spec.dashboard_url,
        )


class GeminiAccountAdapter(AccountUsageAdapter):
    _exhausted_pattern = re.compile(
        r"(?P<month>\d{2})(?P<day>\d{2}) (?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}).*?"
        r"RESOURCE_EXHAUSTED.*?Resets in (?P<duration>(?:\d+h)?(?:\d+m)?(?:\d+s)?)",
        re.IGNORECASE,
    )

    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)
        installation = self._find_antigravity()
        if installation is None and not any(os.environ.get(key) for key in self.spec.env_keys):
            return self.disconnected("Gemini/Antigravity não detectado e nenhuma API key configurada.", "antigravity_local")
        log_path = self._antigravity_log()
        exhausted = self._latest_exhaustion(log_path) if log_path else None
        if exhausted and exhausted > datetime.now().astimezone():
            return ProviderAccountUsage(
                provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
                family=self.spec.family, status=AccountConnectionStatus.LIMITED, adapter="antigravity_local",
                quota_supported=True,
                windows=[QuotaWindow(
                    quota_id="antigravity:individual", label="Antigravity · janela individual",
                    used_percent=100.0, remaining_percent=0.0, resets_at=exhausted.isoformat(),
                )],
                message="Antigravity autenticado e atualmente limitado; reset extraído do erro estruturado local.",
                dashboard_url=self.spec.dashboard_url,
            )
        if installation is not None and log_path is not None and log_path.exists():
            return ProviderAccountUsage(
                provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
                family=self.spec.family, status=AccountConnectionStatus.CONNECTED, adapter="antigravity_local",
                quota_supported=False,
                message="Sessão Antigravity detectada; o percentual atual não é publicado localmente. Configure snapshot para exibi-lo.",
                dashboard_url=self.spec.dashboard_url,
            )
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
            family=self.spec.family, status=AccountConnectionStatus.CONNECTED, adapter="gemini_api_key",
            quota_supported=False, message="Gemini API configurada; quotas detalhadas ficam no AI Studio/Cloud Monitoring.",
            dashboard_url=self.spec.dashboard_url,
        )

    @staticmethod
    def _find_antigravity() -> Optional[Path]:
        executable = shutil.which("antigravity")
        if executable:
            return Path(executable)
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidate = Path(local_app_data) / "Programs" / "antigravity" / "Antigravity.exe"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _antigravity_log() -> Optional[Path]:
        app_data = os.environ.get("APPDATA")
        return Path(app_data) / "Antigravity" / "logs" / "language_server.log" if app_data else None

    @classmethod
    def _latest_exhaustion(cls, log_path: Path) -> Optional[datetime]:
        try:
            with log_path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 1_500_000))
                text = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        matches = list(cls._exhausted_pattern.finditer(text))
        if not matches:
            return None
        match = matches[-1]
        now = datetime.now().astimezone()
        try:
            observed = datetime(
                now.year, int(match.group("month")), int(match.group("day")), int(match.group("hour")),
                int(match.group("minute")), int(match.group("second")), tzinfo=now.tzinfo,
            )
        except ValueError:
            return None
        if observed > now + timedelta(days=2):
            observed = observed.replace(year=now.year - 1)
        duration_text = match.group("duration")
        hours = re.search(r"(\d+)h", duration_text)
        minutes = re.search(r"(\d+)m", duration_text)
        seconds = re.search(r"(\d+)s", duration_text)
        return observed + timedelta(
            hours=int(hours.group(1)) if hours else 0,
            minutes=int(minutes.group(1)) if minutes else 0,
            seconds=int(seconds.group(1)) if seconds else 0,
        )


class EnvironmentAccountAdapter(AccountUsageAdapter):
    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)
        if not any(os.environ.get(key) for key in self.spec.env_keys):
            return self.disconnected("Conta não configurada neste ambiente.", "environment")
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
            family=self.spec.family, status=AccountConnectionStatus.CONNECTED, adapter="environment",
            quota_supported=False,
            message="Credencial detectada; esta plataforma não expõe a quota do plano por API pública compatível.",
            dashboard_url=self.spec.dashboard_url,
        )


class OllamaAccountAdapter(AccountUsageAdapter):
    def inspect(self) -> ProviderAccountUsage:
        try:
            from urllib.request import urlopen
            with urlopen("http://localhost:11434/api/tags", timeout=1.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            model_count = len(payload.get("models", [])) if isinstance(payload, dict) else 0
            return ProviderAccountUsage(
                provider_id=self.spec.provider_id, provider_name=self.spec.provider_name,
                family=self.spec.family, status=AccountConnectionStatus.CONNECTED, adapter="ollama_api",
                plan="local $0", quota_supported=False,
                message=f"Cluster local online com {model_count} modelos; sem quota de assinatura.",
                dashboard_url=self.spec.dashboard_url,
            )
        except Exception:
            return self.disconnected("Ollama local não respondeu.", "ollama_api")


def build_default_adapters(snapshot_dir: Path) -> Iterable[AccountUsageAdapter]:
    for spec in DEFAULT_PROVIDER_SPECS:
        if spec.provider_id == "openai":
            yield CodexAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "xai":
            yield GrokAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "google":
            yield GeminiAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "ollama":
            yield OllamaAccountAdapter(spec, snapshot_dir)
        else:
            yield EnvironmentAccountAdapter(spec, snapshot_dir)
