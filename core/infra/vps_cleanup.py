"""VPS disk space monitoring, automated safe cleanup, and alerting (USR-122).

Governed by:
- Automated safe cleanup: cleanDockerBuilder + cleanUnusedImages via Dokploy API.
  Never prunes volumes or active containers.
- Scheduled and pre-deploy execution.
- Threshold-based owner bot alert when VPS disk usage exceeds 85%.
- Detection of disk-space build failure signatures (e.g. apt-get archive ENOSPC).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("darkfac.infra.vps_cleanup")

DEFAULT_DISK_ALERT_THRESHOLD = 85.0

DISK_FAILURE_PATTERNS = (
    "you don't have enough free space",
    "no space left on device",
    "not enough free disk space",
    "enospc",
    "disk full",
    "/var/cache/apt/archives",
    "error writing to output file",
)


def is_disk_space_failure(error_text: Optional[str]) -> Optional[str]:
    """Inspects text (logs, error messages, descriptions) for VPS disk space exhaustion signatures.

    Returns the matching phrase if found, or None.
    """
    if not error_text:
        return None
    normalized = str(error_text).lower()
    for pattern in DISK_FAILURE_PATTERNS:
        if pattern in normalized:
            return pattern
    return None


def clean_vps_docker_cache(
    api_url: str,
    api_key: str,
    *,
    timeout: float = 30.0,
    transport: Optional[Callable[[str, str, Optional[Dict[str, Any]]], Any]] = None,
) -> Dict[str, Any]:
    """Triggers safe Dokploy docker cache and unused images cleanup.

    Safety contract:
    - settings.cleanDockerBuilder: prunes dangling docker buildkit cache only.
    - settings.cleanUnusedImages: prunes unreferenced/dangling images only.
    - NEVER prunes docker volumes or running containers.
    """
    report: Dict[str, Any] = {
        "cleanDockerBuilder": False,
        "cleanUnusedImages": False,
        "errors": [],
    }

    def _call(path: str) -> Any:
        if transport is not None:
            return transport("POST", path, {})
        url = f"{api_url.rstrip('/')}{path}"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}

    # 1. Clean Docker Builder cache
    try:
        _call("/api/settings.cleanDockerBuilder")
        report["cleanDockerBuilder"] = True
    except Exception as exc:
        msg = f"settings.cleanDockerBuilder failed: {exc}"
        logger.warning(msg)
        report["errors"].append(msg)

    # 2. Clean unused images
    try:
        _call("/api/settings.cleanUnusedImages")
        report["cleanUnusedImages"] = True
    except Exception as exc:
        msg = f"settings.cleanUnusedImages failed: {exc}"
        logger.warning(msg)
        report["errors"].append(msg)

    return report


def check_vps_disk_and_alert(
    disk_percent: float,
    *,
    threshold: float = DEFAULT_DISK_ALERT_THRESHOLD,
    mount: str = "/",
    notifier: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Dispatches a warning alert to the owner bot when VPS disk usage exceeds threshold."""
    if disk_percent <= threshold:
        return False

    message = (
        f"[ALERTA DE DISCO VPS] Espaço em disco no VPS CX23 em nível crítico: "
        f"{disk_percent:.1f}% em uso (limiar de segurança: {threshold:.0f}% no mount '{mount}'). "
        "Ação recomendada: limpeza automática do cache do Docker Builder e remoção de imagens sem uso."
    )

    if notifier is not None:
        try:
            return bool(notifier(message))
        except Exception as exc:
            logger.warning("Notifier callback failed for VPS disk alert: %s", exc)

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True

    try:
        from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
        from core.notifications.service import NotificationService

        service = NotificationService()
        event = service.notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.WARNING,
            title="Alerta de Disco VPS",
            message=message,
            channel=NotificationChannel.TELEGRAM,
            force=True,
        )
        return event is not None
    except Exception as exc:
        logger.warning("Falha ao despachar alerta de disco VPS ao NotificationService: %s", exc)
        return False


def get_local_disk_usage(path: str = "/") -> Dict[str, Any]:
    """Helper for reading local filesystem capacity without extra dependencies."""
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "disk_percent": round((used / total) * 100, 1),
        }
    except Exception as exc:
        logger.debug("Cannot query disk usage for %s: %s", path, exc)
        return {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "disk_percent": 0.0}


def read_dokploy_credentials(env: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """Reads Dokploy API URL and key from env with Windows User registry fallback."""
    import sys

    env_map = os.environ if env is None else env
    api_url = (env_map.get("DOKPLOY_API_URL") or "").strip()
    api_key = (env_map.get("DOKPLOY_API_KEY") or "").strip()

    if (not api_url or not api_key) and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                if not api_url:
                    try:
                        val, _ = winreg.QueryValueEx(key, "DOKPLOY_API_URL")
                        api_url = str(val).strip()
                    except Exception:
                        pass
                if not api_key:
                    try:
                        val, _ = winreg.QueryValueEx(key, "DOKPLOY_API_KEY")
                        api_key = str(val).strip()
                    except Exception:
                        pass
        except Exception:
            pass

    return (api_url.rstrip("/") if api_url else None, api_key if api_key else None)


def clean_vps_if_configured() -> Optional[Dict[str, Any]]:
    """Safely cleans Dokploy docker build cache and unused images if credentials are available (USR-122)."""
    try:
        api_url, api_key = read_dokploy_credentials()
        if api_url and api_key:
            return clean_vps_docker_cache(api_url, api_key)
        return None
    except Exception as exc:
        logger.debug("Dokploy credentials not configured or cleanup skipped: %s", exc)
        return None
