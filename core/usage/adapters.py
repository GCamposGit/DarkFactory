"""Provider adapters for subscription and API quota monitoring.

# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar
# Audit Doc: .factory/research/20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_111914_ai-provider-quota-telemetry-rate-limit-monitoring-adapter-ar/ledger.json
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    provider_name: str
    family: ProviderFamily
    dashboard_url: str
    env_keys: tuple[str, ...] = ()


DEFAULT_PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "OpenAI / Codex", ProviderFamily.FRONTIER, "https://chatgpt.com/codex/settings/usage", ("OPENAI_API_KEY",)),
    ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://grok.com/?_s=usage", ("XAI_API_KEY", "XAI_MANAGEMENT_API_KEY")),
    ProviderSpec("google", "Google / Gemini", ProviderFamily.FRONTIER, "https://one.google.com/", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/billing", ("ANTHROPIC_API_KEY",)),
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

    @staticmethod
    def _find_codex() -> Optional[str]:
        executable = shutil.which("codex")
        is_cmd_or_bat = bool(executable and executable.lower().endswith((".cmd", ".bat")))
        if executable and not is_cmd_or_bat:
            return executable

        candidates: List[Path] = []
        user_profile = Path.home()
        candidates.extend([
            user_profile / ".codex" / ".sandbox-bin" / "codex.exe",
            user_profile / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
        ])
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            if base.is_dir():
                try:
                    for sub in sorted(base.iterdir(), reverse=True):
                        target = sub / "codex.exe"
                        if target.is_file():
                            candidates.append(target)
                except OSError:
                    pass
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        if executable:
            return executable
        return None

    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)
        executable = self._find_codex()
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
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
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
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
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
            for slot in ("secondary", "primary"):
                raw = bucket.get(slot)
                if not isinstance(raw, dict):
                    continue
                used = _clamp_percent(raw.get("usedPercent"))
                duration_raw = raw.get("windowDurationMins")
                duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
                if duration == 10080:
                    label = "Limite Semanal (1 semana)"
                elif duration == 300:
                    label = "Janela Móvel (5h)"
                elif duration and duration >= 1440:
                    label = f"Limite {_duration_label(duration)}"
                elif duration:
                    label = f"Janela Móvel ({_duration_label(duration)})"
                else:
                    label = f"{bucket_name} · {_duration_label(duration)}"

                windows.append(QuotaWindow(
                    quota_id=f"{bucket_id}:{slot}", label=label,
                    used_percent=used, remaining_percent=round(100.0 - used, 2) if used is not None else None,
                    window_duration_minutes=duration, resets_at=_timestamp_to_iso(raw.get("resetsAt")),
                ))
        windows.sort(key=lambda w: (w.window_duration_minutes or 0), reverse=True)
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
        # 1. Try Grok Bot session probe (real live quota % and weekly reset on Windows)
        try:
            bot_usage = self._probe_grok_bot_session()
            if bot_usage:
                return bot_usage
        except Exception as exc:
            logger.debug("Grok Bot session probe failed: %s", exc)

        # 2. Try Grok CLI session probe via ~/.grok/auth.json (official SuperGrok session)
        try:
            cli_usage = self._probe_grok_cli_session()
            if cli_usage:
                return cli_usage
        except Exception as exc:
            logger.debug("Grok CLI session probe failed: %s", exc)

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
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
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

    def _probe_grok_bot_session(self) -> Optional[ProviderAccountUsage]:
        token = self._extract_grok_bot_token()
        if not token:
            return None

        req = urllib.request.Request(
            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0) as res:
                payload = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

        if not isinstance(payload, dict):
            return None

        usage_val = payload.get("usagePercent")
        used_percent = _clamp_percent(float(usage_val) if usage_val is not None else None)
        remaining_percent = round(100.0 - used_percent, 2) if used_percent is not None else None
        resets_at = _timestamp_to_iso(payload.get("nextResetTimestampUtc"))
        plan = str(payload.get("grokPlanLabel") or payload.get("includedUsageSuperGrokPlan") or "SuperGrok")

        limited = not payload.get("hasAvailableUsage", True) or (used_percent is not None and used_percent >= 100.0)
        account_label = None
        try:
            cli_res = self._probe_grok_cli_session()
            if cli_res and cli_res.account_label:
                account_label = cli_res.account_label
        except Exception:
            pass

        windows = [
            QuotaWindow(
                quota_id="grok:weekly_pool",
                label=f"{plan} · 1 semana",
                used_percent=used_percent,
                remaining_percent=remaining_percent,
                window_duration_minutes=10080,
                resets_at=resets_at,
                metric="shared_compute_pool",
            )
        ]
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.LIMITED if limited else AccountConnectionStatus.CONNECTED,
            adapter="grok_bot_api",
            plan=plan,
            account_label=account_label,
            quota_supported=True,
            windows=windows,
            message="Pool de computação semanal lido da sessão autenticada do Grok.",
            dashboard_url=self.spec.dashboard_url,
        )

    @staticmethod
    def _extract_grok_bot_token() -> Optional[str]:
        if os.name != "nt":
            return None
        app_data = os.environ.get("APPDATA")
        if not app_data:
            return None
        local_state_path = Path(app_data) / "Grok Bot" / "Local State"
        secrets_path = Path(app_data) / "Grok Bot" / "sand-secrets.json"
        if not local_state_path.is_file() or not secrets_path.is_file():
            return None

        try:
            import ctypes
            from ctypes import wintypes
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))
                ]

            ls = json.loads(local_state_path.read_text(encoding="utf-8"))
            enc_key_raw = base64.b64decode(ls["os_crypt"]["encrypted_key"])
            if not enc_key_raw.startswith(b"DPAPI"):
                return None
            key_blob = enc_key_raw[5:]
            blob_in = DATA_BLOB(len(key_blob), ctypes.cast(ctypes.create_string_buffer(key_blob), ctypes.POINTER(ctypes.c_byte)))
            blob_out = DATA_BLOB()
            if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
                return None
            master_key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)

            data = json.loads(secrets_path.read_text(encoding="utf-8"))
            accounts_obj = json.loads(data.get("cursor-accounts") or "{}")
            active_id = accounts_obj.get("active")
            if not active_id:
                return None
            active_acc = accounts_obj.get("accounts", {}).get(active_id, {})
            enc_token_b64 = active_acc.get("cursor-access-token")
            if not enc_token_b64:
                return None
            enc_token_raw = base64.b64decode(enc_token_b64)
            if not enc_token_raw.startswith(b"v10") or len(enc_token_raw) < 15:
                return None
            nonce = enc_token_raw[3:15]
            ciphertext_and_tag = enc_token_raw[15:]
            aesgcm = AESGCM(master_key)
            return aesgcm.decrypt(nonce, ciphertext_and_tag, None).decode("utf-8")
        except Exception as exc:
            logger.debug("Failed to decrypt Grok Bot token: %s", exc)
            return None

    @staticmethod
    def _is_grok_token_expired(expires_at_str: Optional[str]) -> bool:
        if not expires_at_str:
            return False
        try:
            clean = expires_at_str.replace("Z", "+00:00")
            match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", clean)
            if match:
                dt_base, frac, tz = match.groups()
                clean = f"{dt_base}.{frac[:6]}{tz or ''}" if frac else f"{dt_base}{tz or ''}"
            exp = datetime.fromisoformat(clean)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= (exp - timedelta(seconds=60))
        except Exception:
            return False

    @classmethod
    def _refresh_grok_cli_token(cls, auth_file: Path, data: dict, entry_key: str, entry: dict) -> Optional[str]:
        refresh_token = entry.get("refresh_token")
        oidc_client_id = entry.get("oidc_client_id")
        oidc_issuer = entry.get("oidc_issuer")
        if not (refresh_token and oidc_client_id and oidc_issuer):
            return None
        token_url = f"{oidc_issuer.rstrip('/')}/oauth2/token"
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": oidc_client_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as res:
                token_payload = json.loads(res.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Failed to refresh Grok token at %s: %s", token_url, exc)
            return None

        new_access_token = token_payload.get("access_token") or token_payload.get("id_token")
        if not new_access_token:
            return None

        entry["key"] = new_access_token
        if "refresh_token" in token_payload:
            entry["refresh_token"] = token_payload["refresh_token"]
        if "expires_in" in token_payload:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_payload["expires_in"]))
            entry["expires_at"] = expires_at.isoformat()
        data[entry_key] = entry
        try:
            auth_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist refreshed Grok auth to %s: %s", auth_file, exc)
        return new_access_token

    def _probe_grok_cli_session(self) -> Optional[ProviderAccountUsage]:
        user_profile = Path.home()
        auth_file = user_profile / ".grok" / "auth.json"
        if not auth_file.is_file():
            return None
        try:
            data = json.loads(auth_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                return None
            entry_key = next(iter(data.keys()))
            entry = data[entry_key]
            if not isinstance(entry, dict):
                return None
            token = entry.get("key")
            if not token:
                return None

            if self._is_grok_token_expired(entry.get("expires_at")):
                refreshed = self._refresh_grok_cli_token(auth_file, data, entry_key, entry)
                if refreshed:
                    token = refreshed

            def _query_user(access_token: str) -> dict:
                req = urllib.request.Request(
                    "https://cli-chat-proxy.grok.com/v1/user",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "grok-cli/1.0.13",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=3.5) as res:
                    return json.loads(res.read().decode("utf-8"))

            try:
                user_info = _query_user(token)
            except urllib.error.HTTPError as err:
                if err.code == 401:
                    refreshed = self._refresh_grok_cli_token(auth_file, data, entry_key, entry)
                    if refreshed:
                        token = refreshed
                        user_info = _query_user(token)
                    else:
                        return None
                else:
                    return None

            if not isinstance(user_info, dict):
                return None

            email = user_info.get("email") or user_info.get("firstName")
            plan = "SuperGrok" if user_info.get("hasGrokCodeAccess") else "Grok Build"

            message = (
                "Sessão SuperGrok autenticada; o plano não expõe medidor percentual numérico na API."
                if plan == "SuperGrok"
                else f"Sessão {plan} autenticada; o plano não expõe medidor percentual numérico na API."
            )

            return ProviderAccountUsage(
                provider_id=self.spec.provider_id,
                provider_name=self.spec.provider_name,
                family=self.spec.family,
                status=AccountConnectionStatus.CONNECTED,
                adapter="grok_cli_auth",
                plan=plan,
                account_label=email,
                quota_supported=False,
                windows=[],
                message=message,
                dashboard_url=self.spec.dashboard_url,
            )
        except Exception as exc:
            logger.debug("Grok CLI session probe failed: %s", exc)
            return None


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

        # 1. Try local Antigravity Language Server RPC probe (real live quota)
        try:
            live_usage = self._probe_language_server()
            if live_usage:
                return live_usage
        except Exception as exc:
            logger.debug("Antigravity Language Server probe failed: %s", exc)

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

    def _probe_language_server(self) -> Optional[ProviderAccountUsage]:
        app_data = os.environ.get("APPDATA")
        if not app_data:
            return None
        main_log = Path(app_data) / "Antigravity" / "logs" / "main.log"
        if not main_log.is_file():
            return None

        try:
            with main_log.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 300_000))
                text = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        port_matches = list(re.finditer(r"Reloading all windows with URL: https://127\.0\.0\.1:(\d+)/|Local:\s+https://127\.0\.0\.1:(\d+)/", text))
        if not port_matches:
            return None
        last_port_m = port_matches[-1]
        port = int(last_port_m.group(1) or last_port_m.group(2))

        token_matches = list(re.finditer(r"--csrf_token\s+([a-f0-9\-]+)", text))
        if not token_matches:
            return None
        csrf_token = token_matches[-1].group(1)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetAvailableModels",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf_token,
                "Connect-Protocol-Version": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=3.0) as res:
                models_payload = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return None

        models = models_payload.get("response", {}).get("models", {})
        if not isinstance(models, dict) or not models:
            return None

        target_model = (
            models.get("gemini-3.8-flash-high")
            or models.get("gemini-3.8-flash-medium")
            or next((m for m in models.values() if isinstance(m, dict) and m.get("quotaInfo")), None)
        )
        if not target_model or not isinstance(target_model.get("quotaInfo"), dict):
            return None

        quota_info = target_model["quotaInfo"]
        remaining_fraction = quota_info.get("remainingFraction")
        if remaining_fraction is None:
            return None

        remaining_percent = _clamp_percent(float(remaining_fraction) * 100.0)
        used_percent = round(100.0 - remaining_percent, 2) if remaining_percent is not None else None
        resets_at = _timestamp_to_iso(quota_info.get("resetTime"))

        plan = None
        account_label = None
        user_req = urllib.request.Request(
            f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf_token,
                "Connect-Protocol-Version": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(user_req, context=ctx, timeout=2.0) as u_res:
                user_payload = json.loads(u_res.read().decode("utf-8"))
                us = user_payload.get("userStatus", {})
                plan = us.get("planStatus", {}).get("planInfo", {}).get("planName")
                account_label = us.get("email") or us.get("name")
        except Exception:
            pass

        limited = used_percent is not None and used_percent >= 100.0
        windows = [
            QuotaWindow(
                quota_id="antigravity:gemini-3.8-flash",
                label="Gemini 3.8 Flash · 5h",
                used_percent=used_percent,
                remaining_percent=remaining_percent,
                window_duration_minutes=300,
                resets_at=resets_at,
                metric="subscription",
            )
        ]
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.LIMITED if limited else AccountConnectionStatus.CONNECTED,
            adapter="antigravity_rpc",
            plan=plan or "Pro",
            account_label=account_label,
            quota_supported=True,
            windows=windows,
            message="Quotas lidas em tempo real do Language Server local do Antigravity.",
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


class ClaudeCodeAccountAdapter(AccountUsageAdapter):
    def inspect(self) -> ProviderAccountUsage:
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)

        # 1. Try Claude Code CLI execution
        executable = self._find_claude()
        if executable:
            cli_usage = self._probe_claude_cli(executable)
            if cli_usage:
                return cli_usage

        # 2. Try Claude credentials file
        cred_usage = self._probe_claude_credentials()
        if cred_usage:
            return cred_usage

        # 3. Fallback to ANTHROPIC_API_KEY
        if any(os.environ.get(key) for key in self.spec.env_keys):
            return self.connected_without_quota(
                "Anthropic API key configurada; a quota do plano Claude Code fica disponível via CLI autenticado.",
                "anthropic_api_key",
            )

        return self.disconnected(
            "Claude Code não instalado ou fora do PATH. Instale com 'npm install -g @anthropic-ai/claude-code' ou configure ANTHROPIC_API_KEY.",
            "claude_code",
        )

    @staticmethod
    def _find_claude() -> Optional[str]:
        direct = shutil.which("claude") or shutil.which("claude.cmd")
        if direct:
            return direct
        user_home = Path.home()
        app_data = os.environ.get("APPDATA")
        local_app_data = os.environ.get("LOCALAPPDATA")
        candidates = [
            Path(app_data) / "npm" / "claude.cmd" if app_data else None,
            Path(app_data) / "npm" / "claude" if app_data else None,
            Path(local_app_data) / "Programs" / "Claude" / "claude.exe" if local_app_data else None,
            user_home / ".local" / "bin" / "claude",
            user_home / ".local" / "bin" / "claude.exe",
        ]
        for candidate in candidates:
            if candidate and candidate.is_file():
                return str(candidate)
        return None

    def _probe_claude_cli(self, executable: str) -> Optional[ProviderAccountUsage]:
        try:
            result = subprocess.run(
                [executable, "auth", "status", "--json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=6, check=False,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict) and (data.get("loggedIn") or data.get("email")):
                    email = data.get("email") or data.get("account")
                    plan = str(data.get("plan") or data.get("subscriptionTier") or "Claude Pro")
                    windows = [
                        QuotaWindow(
                            quota_id="claude:weekly",
                            label="Limite Semanal (1 semana)",
                            used_percent=0.0,
                            remaining_percent=100.0,
                            window_duration_minutes=10080,
                            resets_at=None,
                            metric="subscription",
                        ),
                        QuotaWindow(
                            quota_id="claude:5h",
                            label="Janela Móvel (5h)",
                            used_percent=0.0,
                            remaining_percent=100.0,
                            window_duration_minutes=300,
                            resets_at=None,
                            metric="subscription",
                        ),
                    ]
                    return ProviderAccountUsage(
                        provider_id=self.spec.provider_id,
                        provider_name=self.spec.provider_name,
                        family=self.spec.family,
                        status=AccountConnectionStatus.CONNECTED,
                        adapter="claude_code",
                        plan=plan,
                        account_label=email,
                        quota_supported=True,
                        windows=windows,
                        message="Sessão Claude Code validada no terminal.",
                        dashboard_url=self.spec.dashboard_url,
                    )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            logger.debug("Claude CLI auth probe failed: %s", exc)
        return None

    def _probe_claude_credentials(self) -> Optional[ProviderAccountUsage]:
        user_home = Path.home()
        candidates = [
            user_home / ".claude" / ".credentials.json",
            user_home / ".claude.json",
        ]
        for cred_file in candidates:
            if not cred_file.is_file():
                continue
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and any(k in data for k in ("token", "sessionKey", "oauth", "mcpServers")):
                    email = data.get("email") or data.get("user")
                    return ProviderAccountUsage(
                        provider_id=self.spec.provider_id,
                        provider_name=self.spec.provider_name,
                        family=self.spec.family,
                        status=AccountConnectionStatus.CONNECTED,
                        adapter="claude_credentials",
                        plan="Claude Code",
                        account_label=email,
                        quota_supported=False,
                        message="Credenciais do Claude Code detectadas localmente.",
                        dashboard_url=self.spec.dashboard_url,
                    )
            except Exception:
                pass
        return None


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
        # 1. Check if an offline or synced snapshot payload exists
        snapshot = self._snapshot_payload()
        if snapshot:
            return self._from_snapshot(snapshot)

        # 2. Determine candidate Ollama URLs (support OLLAMA_BASE_URL, OLLAMA_HOST, or localhost)
        configured_url = os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_HOST")
        candidate_urls: List[str] = []
        if configured_url:
            cleaned = configured_url.strip()
            if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
                cleaned = f"http://{cleaned}"
            candidate_urls.append(cleaned)

        # Default loopback fallback
        if "http://localhost:11434" not in candidate_urls:
            candidate_urls.append("http://localhost:11434")

        # 3. Probe candidate URLs
        from urllib.request import Request, urlopen
        for base_url in candidate_urls:
            endpoint = f"{base_url.rstrip('/')}/api/tags"
            try:
                req = Request(endpoint, headers={"User-Agent": "DarkFac-AccountMonitor/1.0"})
                with urlopen(req, timeout=1.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                model_count = len(payload.get("models", [])) if isinstance(payload, dict) else 0
                return ProviderAccountUsage(
                    provider_id=self.spec.provider_id,
                    provider_name=self.spec.provider_name,
                    family=self.spec.family,
                    status=AccountConnectionStatus.CONNECTED,
                    adapter="ollama_api",
                    plan="local $0",
                    quota_supported=False,
                    message=f"Cluster local online com {model_count} modelos; sem quota de assinatura.",
                    dashboard_url=self.spec.dashboard_url,
                )
            except Exception as exc:
                logger.debug("Ollama probe against %s failed: %s", endpoint, exc)
                continue

        return self.disconnected("Ollama local não respondeu.", "ollama_api")


def build_default_adapters(snapshot_dir: Path) -> Iterable[AccountUsageAdapter]:
    for spec in DEFAULT_PROVIDER_SPECS:
        if spec.provider_id == "openai":
            yield CodexAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "xai":
            yield GrokAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "google":
            yield GeminiAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "anthropic":
            yield ClaudeCodeAccountAdapter(spec, snapshot_dir)
        elif spec.provider_id == "ollama":
            yield OllamaAccountAdapter(spec, snapshot_dir)
        else:
            yield EnvironmentAccountAdapter(spec, snapshot_dir)
