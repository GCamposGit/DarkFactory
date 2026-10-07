"""VPS disk space monitoring, automated safe cleanup, and alerting (USR-122, hardened after the 2026-10-06 incident).

Governed by:
- Automated safe cleanup: settings.cleanDockerBuilder + settings.cleanUnusedImages via the Dokploy API.
  Never prunes volumes or running containers. NOTE (verified against Dokploy source): these endpoints run
  `docker builder prune --all --force` and `docker image prune --all --force` -- NOT dangling-only and
  with no age filter -- so they may only run while no deployment is in progress (see
  `find_running_deployments`), otherwise a just-built, not-yet-started image could be removed.
- Policy-driven execution (`clean_vps_if_needed`): pre-deploy, post-deploy and scheduled, never blind.
- Two-level owner bot alert on the HOST disk: 80% (warning) and 90% (critical), at most once per level
  per 6h, with top consumers (`evaluate_and_alert_disk`).
- Detection of disk-space build failure signatures (e.g. apt-get archive ENOSPC).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("darkfac.infra.vps_cleanup")

DEFAULT_DISK_ALERT_THRESHOLD = 85.0

# Two-level alerting (owner order after the 2026-10-06 100%-disk incident).
DISK_WARNING_PERCENT = 80.0
DISK_CRITICAL_PERCENT = 90.0
ALERT_COOLDOWN_SECONDS = 6 * 3600.0

# Cleanup policy: act when free space drops below this many GB (40 GB disk -> 80% used).
DEFAULT_MIN_FREE_GB = 8.0
# Below this much free space a build is about to fail anyway: prune even if a deploy is running.
EMERGENCY_FREE_GB = 2.0
MIN_FREE_GB_ENV = "DARKFAC_VPS_MIN_FREE_GB"
# Dokploy's prune endpoints wait up to 300s for docker to be idle before running (dockerSafeExec).
PRUNE_TIMEOUT_SECONDS = 420.0

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
    timeout: float = PRUNE_TIMEOUT_SECONDS,
    transport: Optional[Callable[[str, str, Optional[Dict[str, Any]]], Any]] = None,
    builder: bool = True,
    images: bool = True,
) -> Dict[str, Any]:
    """Triggers Dokploy docker build-cache and unused-images cleanup.

    Safety contract (verified against the Dokploy source, 2026-10):
    - settings.cleanDockerBuilder runs `docker builder prune --all --force` (ALL unused build cache).
    - settings.cleanUnusedImages runs `docker image prune --all --force` (ALL images not used by a container).
    - NEVER prunes docker volumes or running containers.
    - Because there is no age filter, callers must not run this while a deployment is being built
      (use `clean_vps_if_needed`, which checks that first).
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
    if builder:
        try:
            _call("/api/settings.cleanDockerBuilder")
            report["cleanDockerBuilder"] = True
        except Exception as exc:
            msg = f"settings.cleanDockerBuilder failed: {exc}"
            logger.warning(msg)
            report["errors"].append(msg)

    # 2. Clean unused images
    if images:
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


# ---------------------------------------------------------------------------
# Dokploy API transport + deployment-in-progress detection
# ---------------------------------------------------------------------------

Transport = Callable[[str, str, Optional[Dict[str, Any]]], Any]


def make_dokploy_transport(api_url: str, api_key: str, *, timeout: float = 30.0) -> Transport:
    """Minimal urllib transport for the Dokploy REST API; the key only ever goes in `x-api-key`."""

    base = api_url.rstrip("/")

    def transport(method: str, path: str, body: Optional[Dict[str, Any]]) -> Any:
        url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"x-api-key": api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Dokploy API HTTP {exc.code} for {method} {path}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Dokploy API unreachable for {method} {path}: {exc.reason}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    return transport


def _iter_service_containers(projects: Any) -> List[Dict[str, Any]]:
    """Flattens a project.all payload into the dicts that carry compose/applications lists."""
    if isinstance(projects, dict):
        projects = projects.get("data") if isinstance(projects.get("data"), list) else []
    containers: List[Dict[str, Any]] = []
    for project in projects or []:
        if not isinstance(project, dict):
            continue
        environments = project.get("environments")
        if isinstance(environments, list) and environments:
            containers.extend(env for env in environments if isinstance(env, dict))
        else:
            containers.append(project)
    return containers


def find_running_deployments(transport: Transport) -> List[str]:
    """Names of Dokploy services whose status is `running` (a deployment is being built/started).

    Raises on API failure so callers can fail closed (skip the prune) instead of guessing.
    """
    projects = transport("GET", "/api/project.all", None)
    if not isinstance(projects, (list, dict)):
        raise RuntimeError("Unexpected /api/project.all response shape")
    running: List[str] = []
    for container in _iter_service_containers(projects):
        for compose in container.get("compose") or []:
            if str(compose.get("composeStatus") or "").lower() == "running":
                running.append(str(compose.get("name") or compose.get("composeId") or "compose"))
        for app in container.get("applications") or []:
            if str(app.get("applicationStatus") or "").lower() == "running":
                running.append(str(app.get("name") or app.get("applicationId") or "application"))
    return running


def fetch_docker_disk_usage(transport: Transport) -> List[Dict[str, Any]]:
    """`docker system df` summary via settings.getDockerDiskUsage (best effort; [] on failure)."""
    try:
        result = transport("GET", "/api/settings.getDockerDiskUsage", None)
    except Exception as exc:
        logger.debug("getDockerDiskUsage unavailable: %s", exc)
        return []
    if isinstance(result, dict):
        result = result.get("data") if isinstance(result.get("data"), list) else []
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def format_docker_disk_usage(items: Sequence[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for item in sorted(items, key=lambda i: float(i.get("sizeBytes") or 0), reverse=True):
        kind = str(item.get("type") or "?")
        lines.append(
            f"{kind}: {item.get('size', '?')} ({item.get('totalCount', '?')} itens, "
            f"{item.get('active', '?')} ativos, recuperavel {item.get('reclaimable', '?')})"
        )
    return lines


# ---------------------------------------------------------------------------
# Policy-driven cleanup (pre-deploy / post-deploy / scheduled)
# ---------------------------------------------------------------------------


def running_under_pytest() -> bool:
    """True inside a pytest run: real Dokploy credentials must never be used by tests."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def min_free_gb_from_env(env: Optional[Dict[str, str]] = None) -> float:
    raw = (os.environ if env is None else env).get(MIN_FREE_GB_ENV, "")
    try:
        value = float(str(raw).strip())
        return value if value > 0 else DEFAULT_MIN_FREE_GB
    except ValueError:
        return DEFAULT_MIN_FREE_GB


def clean_vps_if_needed(
    *,
    transport: Optional[Transport] = None,
    disk_probe: Optional[Callable[[], Dict[str, Any]]] = None,
    min_free_gb: Optional[float] = None,
    force_images: bool = False,
    force_builder: bool = False,
    env: Optional[Dict[str, str]] = None,
    stage: str = "scheduled",
) -> Dict[str, Any]:
    """Prunes unused images (and build cache when space is short) if it is safe and useful.

    - Disk low (`free_gb < min_free_gb`): prune unused images AND build cache.
    - `force_images` (post-deploy / every-6h schedule): prune unused images even when the disk is fine
      (superseded image versions pile up with every rebuild); the build cache is kept unless low, so
      rebuilds stay fast.
    - Never while a Dokploy deployment is running (the prune has no age filter and could remove an image
      that was just built but not yet started), except in an emergency (`free_gb <= EMERGENCY_FREE_GB`).
      If the running-deployment check itself fails, the prune is skipped (fail closed).
    - Never touches volumes or running containers. Never raises.
    """
    report: Dict[str, Any] = {"stage": stage, "action": "skipped", "reason": "", "errors": []}
    probe = disk_probe or (lambda: get_local_disk_usage("/"))
    threshold = min_free_gb if min_free_gb is not None else min_free_gb_from_env(env)
    try:
        try:
            before = probe() or {}
        except Exception as exc:
            before = {}
            report["errors"].append(f"disk probe failed: {exc}")
        report["before"] = before
        free_gb = float(before.get("free_gb", 0.0))
        probe_ok = float(before.get("total_gb", 0.0)) > 0
        low = probe_ok and free_gb < threshold
        if not (low or force_images or force_builder):
            report["reason"] = "disk_ok" if probe_ok else "disk_probe_unavailable"
            return report

        if transport is None:
            api_url, api_key = (None, None) if running_under_pytest() else read_dokploy_credentials(env)
            if not (api_url and api_key):
                report["reason"] = "no_credentials"
                logger.warning(
                    "VPS cleanup NOT run (stage=%s, free=%.1f GB): DOKPLOY_API_URL/DOKPLOY_API_KEY are not set "
                    "in this environment.",
                    stage,
                    free_gb,
                )
                return report
            transport = make_dokploy_transport(api_url, api_key, timeout=PRUNE_TIMEOUT_SECONDS)

        emergency = probe_ok and free_gb <= EMERGENCY_FREE_GB
        try:
            running = find_running_deployments(transport)
        except Exception as exc:
            running = ["unknown"]
            report["errors"].append(f"running-deployment check failed: {exc}")
        if running and not emergency:
            report["reason"] = "deploy_in_progress:" + ",".join(running)
            return report

        do_builder = low or force_builder
        result = clean_vps_docker_cache(
            "", "", transport=transport, builder=do_builder, images=True, timeout=PRUNE_TIMEOUT_SECONDS
        )
        report["action"] = "cleaned"
        report["reason"] = "low_disk" if low else "forced"
        report["result"] = result
        report["errors"].extend(result.get("errors", []))
        try:
            after = probe() or {}
            report["after"] = after
            if probe_ok and float(after.get("total_gb", 0.0)) > 0:
                report["freed_gb"] = round(float(after.get("free_gb", 0.0)) - free_gb, 2)
        except Exception as exc:
            report["errors"].append(f"post-clean probe failed: {exc}")
        return report
    except Exception as exc:
        logger.warning("clean_vps_if_needed failed (stage=%s): %s", stage, exc)
        report["errors"].append(str(exc))
        report["reason"] = report["reason"] or "error"
        return report


# ---------------------------------------------------------------------------
# Two-level host disk alert with cooldown and top consumers
# ---------------------------------------------------------------------------


def disk_alert_level(disk_percent: float) -> Optional[str]:
    if disk_percent >= DISK_CRITICAL_PERCENT:
        return "critical"
    if disk_percent >= DISK_WARNING_PERCENT:
        return "warning"
    return None


def _dir_size_bytes(path: Path, max_entries: int = 300_000) -> int:
    total = 0
    seen = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            seen += 1
            if seen > max_entries:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def collect_top_consumers(paths: Sequence[str], *, limit: int = 5) -> List[Tuple[str, int]]:
    """Sizes of the given directories visible from this container (largest first)."""
    sizes: List[Tuple[str, int]] = []
    for raw in paths:
        path = Path(raw)
        try:
            if path.is_dir():
                sizes.append((str(path), _dir_size_bytes(path)))
        except OSError:
            continue
    sizes.sort(key=lambda item: item[1], reverse=True)
    return sizes[:limit]


def format_size_gb(size_bytes: float) -> str:
    return f"{size_bytes / (1024**3):.2f} GB"


def _load_alert_state(state_path: Optional[Path]) -> Dict[str, float]:
    if state_path is None:
        return {}
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_alert_state(state_path: Optional[Path], state: Dict[str, float]) -> None:
    if state_path is None:
        return
    try:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot persist disk alert state to %s: %s", state_path, exc)


def _dispatch_owner_alert(level: str, title: str, message: str) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    try:
        from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
        from core.notifications.service import NotificationService

        service = NotificationService()
        event = service.notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.CRITICAL if level == "critical" else AlertSeverity.WARNING,
            title=title,
            message=message,
            channel=NotificationChannel.TELEGRAM,
            force=True,
        )
        return event is not None
    except Exception as exc:
        logger.warning("Falha ao despachar alerta de disco VPS ao NotificationService: %s", exc)
        return False


def evaluate_and_alert_disk(
    usage: Dict[str, Any],
    *,
    state_path: Optional[Path] = None,
    now: Optional[float] = None,
    consumers: Sequence[str] = (),
    notifier: Optional[Callable[[str, str, str], bool]] = None,
    cooldown_seconds: float = ALERT_COOLDOWN_SECONDS,
    mount: str = "/",
    notes: Sequence[str] = (),
) -> Optional[str]:
    """Sends the owner alert for the HOST disk level (80% warning / 90% critical).

    At most one alert per level per `cooldown_seconds` (state persisted in `state_path`, so it survives
    daemon restarts). A failed delivery is not recorded, so the next cycle retries. Returns the level
    alerted, or None when below 80%, in cooldown, or delivery failed.
    """
    percent = float(usage.get("disk_percent", 0.0))
    level = disk_alert_level(percent)
    if level is None:
        return None
    moment = time.time() if now is None else now
    state = _load_alert_state(state_path)
    last = state.get(level)
    if last is not None and moment - last < cooldown_seconds:
        return None

    label = "CRITICO" if level == "critical" else "AVISO"
    lines = [
        f"[ALERTA DE DISCO VPS - {label}] Disco do host em {percent:.1f}% "
        f"({usage.get('used_gb', '?')} de {usage.get('total_gb', '?')} GB usados, "
        f"{usage.get('free_gb', '?')} GB livres no mount '{mount}').",
        f"Limiares: {DISK_WARNING_PERCENT:.0f}% aviso, {DISK_CRITICAL_PERCENT:.0f}% critico "
        "(no maximo 1 alerta por nivel a cada 6h).",
    ]
    if consumers:
        lines.append("Maiores consumidores:")
        lines.extend(f"- {c}" for c in consumers)
    lines.extend(notes)
    lines.append(
        "Runbook: docs/runbooks/vps_disk.md (emergencia no host: docker builder prune --all --force; "
        "docker image prune --all --force)."
    )
    message = "\n".join(lines)
    title = "Disco VPS critico" if level == "critical" else "Disco VPS em alerta"

    send = notifier or _dispatch_owner_alert
    try:
        delivered = bool(send(level, title, message))
    except Exception as exc:
        logger.warning("Disk alert notifier failed: %s", exc)
        delivered = False
    if not delivered:
        return None
    state[level] = moment
    _save_alert_state(state_path, state)
    return level


def prune_old_files(root: Path, *, max_age_days: float, now: Optional[float] = None) -> Dict[str, Any]:
    """Delete regular files under `root` whose mtime is older than `max_age_days`; drop emptied sub-dirs.

    Never follows symlinks, never leaves `root`, never raises. `max_age_days <= 0` disables it.
    """
    result: Dict[str, Any] = {"root": str(root), "removed_files": 0, "freed_bytes": 0}
    if max_age_days <= 0 or not Path(root).is_dir():
        return result
    cutoff = (time.time() if now is None else now) - max_age_days * 86400.0
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            target = os.path.join(current, name)
            try:
                info = os.lstat(target)
                if info.st_mtime < cutoff and not os.path.islink(target):
                    os.unlink(target)
                    result["removed_files"] += 1
                    result["freed_bytes"] += info.st_size
            except OSError:
                continue
        if Path(current) != Path(root):
            try:
                os.rmdir(current)  # only succeeds when empty
            except OSError:
                pass
    return result
