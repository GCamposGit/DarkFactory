"""Hub service domain: service catalog storage, sessions, health probes and launch."""

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hub.backend.models import (
    ExportCatalogResponse,
    HealthCheckResult,
    HealthStatus,
    ImportCatalogRequest,
    ImportCatalogResponse,
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    ServiceLaunchResponse,
    ServiceUpdate,
)
from core.paths import state_root
from core.integrations.telegram import TelegramGateway


from hub.backend.service_net import SafeRedirectHandler, DisallowedDestinationError, is_destination_allowed


logger = logging.getLogger("darkhub.service")


class CatalogMixin:
    # Shipped seed files a Dokploy volume mount can shadow; never touched: services.json,
    # catalog_meta.json, or anything else the owner may have edited in data_dir.
    _CLOUD_SEED_FILES: tuple[str, ...] = ("default_services.json", "default_prompts.json")

    def _apply_cloud_seed(self) -> None:
        """Overlay shipped catalog/prompt seeds from an image-level seed dir (USR-44).

        In Dokploy, ``darkhub-hub-data`` is a named volume mounted at ``/app/hub/data``,
        so a redeploy that ships an updated ``default_services.json`` /
        ``default_prompts.json`` never reaches the running container: the volume's old
        copies keep shadowing the image's new ones, which means ``_apply_catalog_revisions``
        below never sees new revisions and ``list_prompts`` keeps serving stale content.
        ``DARKHUB_SEED_DIR`` (or an explicit ``seed_dir`` constructor argument, which wins)
        points at an image-only copy of ``hub/data`` outside the volume; when set, this
        copies the two shipped seed files over ``data_dir``'s copies before storage is
        initialized, but only when the seed file exists and its content actually differs.
        This is a cosmetic, fail-open path: any OSError is logged and swallowed so a
        seeding hiccup never prevents the Hub from starting.
        """
        if self.seed_dir is None:
            return
        for filename in self._CLOUD_SEED_FILES:
            seed_file = self.seed_dir / filename
            try:
                if not seed_file.exists():
                    continue
                seed_content = seed_file.read_text(encoding="utf-8")
                target_file = self.data_dir / filename
                if target_file.exists() and target_file.read_text(encoding="utf-8") == seed_content:
                    continue
                target_file.write_text(seed_content, encoding="utf-8")
                logger.info("Refreshed %s from cloud seed dir %s", filename, self.seed_dir)
            except OSError as exc:
                logger.warning("Cloud seed refresh failed for %s: %s", filename, exc)

    def _ensure_storage(self) -> None:
        """Ensures the storage directory and initial files exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._apply_cloud_seed()
        if not self.services_file.exists():
            if self.default_services_file.exists():
                logger.info("Initializing services.json from default_services.json")
                content = self.default_services_file.read_text(encoding="utf-8")
                self.services_file.write_text(content, encoding="utf-8")
            else:
                logger.warning("Default services file missing. Creating empty list.")
                self.services_file.write_text("[]", encoding="utf-8")

        self._migrate_launch_metadata()

    def _resolve_launch_script_path(self, script_name: str) -> Path:
        """Resolve a catalog launch path relative to the shared project root."""
        script_path = Path(script_name)
        if not script_path.is_absolute():
            script_path = self.project_root / script_path
        return script_path.resolve()

    def _migrate_launch_metadata(self) -> None:
        """Refresh missing or obsolete launcher metadata from the shipped catalog.

        The hub catalog is persisted in a Docker volume, so changing the versioned
        seed file alone does not update an already-created ``services.json``. Only
        launcher metadata is migrated, leaving user-facing catalog edits intact.
        """
        if not self.services_file.exists() or not self.default_services_file.exists():
            return

        try:
            current_items = json.loads(self.services_file.read_text(encoding="utf-8"))
            default_items = json.loads(self.default_services_file.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Service catalog migration skipped: %s", exc)
            return

        if not isinstance(current_items, list) or not isinstance(default_items, list):
            return

        defaults_by_id = {
            item.get("id"): item
            for item in default_items
            if isinstance(item, dict) and item.get("id")
        }
        changed = False

        for item in current_items:
            if not isinstance(item, dict):
                continue
            default_item = defaults_by_id.get(item.get("id"))
            if not default_item:
                continue

            default_script = default_item.get("launch_script")
            current_script = item.get("launch_script")
            if default_script and not current_script:
                item["launch_script"] = default_script
                changed = True
            elif default_script and current_script != default_script:
                current_path = self._resolve_launch_script_path(str(current_script))
                default_path = self._resolve_launch_script_path(str(default_script))
                if not current_path.exists() and default_path.exists():
                    item["launch_script"] = default_script
                    changed = True

            if "fallback_urls" not in item and "fallback_urls" in default_item:
                item["fallback_urls"] = default_item["fallback_urls"]
                changed = True

        if self._apply_catalog_revisions(current_items, default_items):
            changed = True

        if changed:
            self._save_services_raw(current_items)
            logger.info("Migrated persisted service launcher metadata from default catalog")


    # Fields a catalog revision may refresh; URLs, favorites and pins stay owned by the user.
    _CATALOG_REVISION_FIELDS: tuple[str, ...] = ("name", "description", "tags", "category", "icon", "color")

    def _apply_catalog_revisions(self, current_items: List[Dict], default_items: List[Dict]) -> bool:
        """Propagate shipped catalog evolutions into a persisted ``services.json`` once (USR-42).

        Default items carrying ``catalog_revision`` newer than the last applied revision are
        appended when absent or have their descriptive fields refreshed. Each revision is
        applied only once, so services deleted by the owner afterwards are not resurrected.
        """
        meta_file = self.data_dir / "catalog_meta.json"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            meta = {}
        applied = str(meta.get("applied_revision") or "") if isinstance(meta, dict) else ""
        newest = applied
        by_id = {item.get("id"): item for item in current_items if isinstance(item, dict)}
        changed = False
        for default_item in default_items:
            revision = str(default_item.get("catalog_revision") or "") if isinstance(default_item, dict) else ""
            if not revision or revision <= applied:
                continue
            newest = max(newest, revision)
            existing = by_id.get(default_item.get("id"))
            if existing is None:
                current_items.append({key: value for key, value in default_item.items() if key != "catalog_revision"})
                changed = True
                continue
            for field in self._CATALOG_REVISION_FIELDS:
                if field in default_item and existing.get(field) != default_item[field]:
                    existing[field] = default_item[field]
                    changed = True
        if newest != applied:
            try:
                meta_file.write_text(json.dumps({"applied_revision": newest}, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("Catalog revision marker not persisted: %s", exc)
        return changed

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _local_service_launch_enabled(self) -> bool:
        """Return whether this process is allowed to spawn local demo services."""
        return self._env_flag(
            "DARKHUB_ENABLE_LOCAL_SERVICE_LAUNCH",
            default=os.getenv("DARKHUB_ENV", "").strip().lower() != "production",
        )

    def _service_url_overrides(self) -> Dict[str, str]:
        """Read optional deployment-time URL overrides for externally hosted services."""
        raw_value = os.getenv("DARKHUB_SERVICE_URL_OVERRIDES", "").strip()
        if not raw_value:
            return {}

        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            logger.warning("Ignoring invalid DARKHUB_SERVICE_URL_OVERRIDES: %s", exc)
            return {}

        if not isinstance(payload, dict):
            logger.warning("Ignoring DARKHUB_SERVICE_URL_OVERRIDES because it is not an object")
            return {}

        overrides: Dict[str, str] = {}
        for service_id, url in payload.items():
            if not isinstance(service_id, str) or not isinstance(url, str):
                continue
            parsed = urllib.parse.urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                logger.warning("Ignoring invalid service URL override for %s", service_id)
                continue
            overrides[service_id] = url.strip()
        return overrides

    def _apply_service_url_overrides(self, items: List[Dict]) -> List[Dict]:
        overrides = self._service_url_overrides()
        if not overrides:
            return items

        resolved_items: List[Dict] = []
        for item in items:
            resolved_item = dict(item)
            service_id = resolved_item.get("id")
            if service_id in overrides:
                resolved_item["url"] = overrides[service_id]
                resolved_item["is_local"] = False
            resolved_items.append(resolved_item)
        return resolved_items

    def _load_services_raw(self) -> List[Dict]:
        try:
            with open(self.services_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data if isinstance(data, list) else []
                return self._apply_service_url_overrides(items)
        except Exception as exc:
            logger.error(f"Failed to read services from {self.services_file}: {exc}")
            return []

    def _save_services_raw(self, items: List[Dict]) -> None:
        temp_file = self.services_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)
            temp_file.replace(self.services_file)
            logger.info(f"Successfully saved {len(items)} services to {self.services_file}")
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink()
            logger.error(f"Error saving services: {exc}")
            raise

    def list_services(
        self,
        category: Optional[ServiceCategory] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        pinned_only: bool = False,
    ) -> List[ServiceItem]:
        raw_items = self._load_services_raw()
        results: List[ServiceItem] = []

        search_term = search.strip().lower() if search else None

        for raw in raw_items:
            try:
                item = ServiceItem(**raw)
            except Exception as exc:
                logger.warning(f"Skipping malformed service item {raw.get('id', 'unknown')}: {exc}")
                continue

            if category and item.category != category:
                continue

            if favorites_only and not item.is_favorite:
                continue

            if pinned_only and not item.pinned:
                continue

            if search_term:
                match_name = search_term in item.name.lower()
                match_desc = search_term in item.description.lower()
                match_tags = any(search_term in t.lower() for t in item.tags)
                match_url = search_term in item.url.lower()
                if not (match_name or match_desc or match_tags or match_url):
                    continue

            results.append(item)

        return results

    def get_service(self, service_id: str) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        for raw in raw_items:
            if raw.get("id") == service_id:
                try:
                    return ServiceItem(**raw)
                except Exception as exc:
                    logger.error(f"Malformed service {service_id}: {exc}")
                    return None
        return None

    def _generate_slug(self, name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return slug or "service"

    def create_service(self, create_data: ServiceCreate) -> ServiceItem:
        raw_items = self._load_services_raw()

        base_id = self._generate_slug(create_data.name)
        new_id = base_id
        counter = 1
        existing_ids = {r.get("id") for r in raw_items}

        while new_id in existing_ids:
            new_id = f"{base_id}-{counter}"
            counter += 1

        is_local = "localhost" in create_data.url or "127.0.0.1" in create_data.url

        new_item = ServiceItem(
            id=new_id,
            name=create_data.name,
            url=create_data.url,
            category=create_data.category,
            description=create_data.description,
            tags=create_data.tags,
            icon=create_data.icon,
            color=create_data.color,
            is_favorite=create_data.is_favorite,
            pinned=create_data.pinned,
            is_local=create_data.is_local or is_local,
            launch_script=create_data.launch_script,
            fallback_urls=create_data.fallback_urls,
        )

        raw_items.append(new_item.model_dump())
        self._save_services_raw(raw_items)
        return new_item

    def update_service(self, service_id: str, update_data: ServiceUpdate) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        updated_item: Optional[ServiceItem] = None

        for i, raw in enumerate(raw_items):
            if raw.get("id") == service_id:
                item = ServiceItem(**raw)
                update_dict = update_data.model_dump(exclude_unset=True)

                if "url" in update_dict:
                    url_val = update_dict["url"]
                    if "is_local" not in update_dict:
                        update_dict["is_local"] = "localhost" in url_val or "127.0.0.1" in url_val

                updated_item = item.model_copy(update=update_dict)
                raw_items[i] = updated_item.model_dump()
                break

        if updated_item:
            self._save_services_raw(raw_items)
        return updated_item

    def delete_service(self, service_id: str) -> bool:
        raw_items = self._load_services_raw()
        initial_count = len(raw_items)
        filtered = [r for r in raw_items if r.get("id") != service_id]

        if len(filtered) < initial_count:
            self._save_services_raw(filtered)
            return True
        return False

    def toggle_favorite(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(is_favorite=not service.is_favorite))

    def toggle_pin(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(pinned=not service.pinned))

    @property
    def session_token(self) -> str:
        """Returns the current active local session token."""
        return self._session_token

    def validate_session(self, token: Optional[str]) -> bool:
        """Validates a provided session token against active local session."""
        if not token or not isinstance(token, str):
            return False
        return secrets.compare_digest(token.strip(), self._session_token)

    def send_owner_login_link(self, now: Optional[float] = None) -> str:
        """Send a one-time DarkHub login link to the owner's Telegram chat.

        Returns "sent", "rate_limited" or "unavailable". The link only ever goes to the authorized
        owner chats already configured for the bots, so triggering it anonymously grants nothing.
        """
        from hub.backend.auth import make_login_code

        current = time.time() if now is None else now
        last = getattr(self, "_last_login_link_at", 0.0)
        if current - last < 60:
            return "rate_limited"
        self._last_login_link_at = current
        base = (os.getenv("DARKHUB_PUBLIC_URL") or os.getenv("DARKHUB_BASE_URL") or "https://darkhub.ggcampos.com").rstrip("/")
        link = f"{base}/api/auth/telegram-callback?code={make_login_code(self._session_token, current)}"
        text = "<b>DarkHub</b>: link de acesso de uso unico (vale 10 minutos). Se nao foi voce, ignore."
        buttons = [[{"text": "Entrar no DarkHub", "url": link}]]
        from core.integrations.telegram import load_telegram_config

        for role in ("owner", "ops"):
            try:
                cfg = load_telegram_config(role=role)
                if not cfg.bot_token:
                    continue
                gateway = TelegramGateway(config=cfg)
                targets = list(cfg.authorized_chat_ids) or list(cfg.authorized_user_ids)
                if any(gateway.send_message(chat_id=chat_id, text=text, buttons=buttons) for chat_id in targets):
                    return "sent"
            except Exception as exc:  # noqa: BLE001 - login must degrade, never crash the Hub
                logger.warning("login link: telegram role %s falhou: %s", role, exc.__class__.__name__)
        return "unavailable"

    def ping_url(self, service_id: str, url: str, timeout_sec: float = 2.5) -> HealthCheckResult:
        """
        Pings a service URL and measures round-trip latency with strict SSRF & redirect guards (DF-08).
        """
        allowed, reason = is_destination_allowed(url)
        if not allowed:
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=HealthStatus.OFFLINE,
                latency_ms=None,
                status_code=None,
                error=f"Destination disallowed by SSRF policy: {reason}",
            )

        start = time.perf_counter()
        opener = urllib.request.build_opener(SafeRedirectHandler)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DarkHub-Ping/1.0 (Headless Health Probe)"},
            method="HEAD",
        )

        try:
            with opener.open(req, timeout=timeout_sec) as response:
                latency = round((time.perf_counter() - start) * 1000, 1)
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.ONLINE,
                    latency_ms=latency,
                    status_code=response.getcode(),
                )
        except DisallowedDestinationError as redirect_err:
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=HealthStatus.OFFLINE,
                latency_ms=None,
                status_code=None,
                error=str(redirect_err),
            )
        except urllib.error.HTTPError as http_err:
            latency = round((time.perf_counter() - start) * 1000, 1)
            # HTTP errors (e.g. 401, 403, 405) still mean the server is reachable and online
            status = HealthStatus.ONLINE if http_err.code < 500 else HealthStatus.DEGRADED
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=status,
                latency_ms=latency,
                status_code=http_err.code,
            )
        except Exception as exc:
            # Fallback retry with GET if HEAD was disallowed by remote
            try:
                start_get = time.perf_counter()
                req_get = urllib.request.Request(
                    url,
                    headers={"User-Agent": "DarkHub-Ping/1.0"},
                    method="GET",
                )
                with opener.open(req_get, timeout=timeout_sec) as response:
                    latency = round((time.perf_counter() - start_get) * 1000, 1)
                    return HealthCheckResult(
                        service_id=service_id,
                        url=url,
                        status=HealthStatus.ONLINE,
                        latency_ms=latency,
                        status_code=response.getcode(),
                    )
            except DisallowedDestinationError as redirect_err:
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.OFFLINE,
                    latency_ms=None,
                    status_code=None,
                    error=str(redirect_err),
                )
            except Exception as get_exc:
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.OFFLINE,
                    latency_ms=None,
                    error=str(get_exc),
                )

    def _probe_service_urls(self, service: ServiceItem, timeout_sec: float) -> HealthCheckResult:
        """Probe a service's primary URL and configured fallbacks in order."""
        result = self.ping_url(service_id=service.id, url=service.url, timeout_sec=timeout_sec)
        if result.status == HealthStatus.ONLINE:
            return result

        for fallback_url in service.fallback_urls:
            fallback_result = self.ping_url(
                service_id=service.id,
                url=fallback_url,
                timeout_sec=timeout_sec,
            )
            if fallback_result.status == HealthStatus.ONLINE:
                return fallback_result

        return result

    def launch_service(
        self,
        service_id: str,
        max_wait_sec: float = 5.0,
        poll_interval: float = 0.2,
    ) -> ServiceLaunchResponse:
        """
        Launches a configured local service script if currently offline.
        Waits until the service is verified online via health probe before returning.
        If already online, returns immediately with already_running status.
        """
        service = self.get_service(service_id)
        if not service:
            raise KeyError(f"Service '{service_id}' not found in catalog")

        if not self._local_service_launch_enabled():
            return ServiceLaunchResponse(
                service_id=service.id,
                url=service.url,
                status="external",
                launched=False,
                message=(
                    f"Service '{service.name}' is hosted externally; "
                    "local process launching is disabled in this environment."
                ),
            )

        # Check if already online
        current_health = self._probe_service_urls(service, timeout_sec=1.0)
        if current_health.status == HealthStatus.ONLINE:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=current_health.url or service.url,
                status="already_running",
                launched=False,
                message=f"Service '{service.name}' is already running and accessible.",
            )

        # Resolve launch script path
        script_name = service.launch_script

        if not script_name:
            raise ValueError(f"Service '{service_id}' does not define a launch_script.")

        script_path = self._resolve_launch_script_path(script_name)

        if not script_path.exists():
            raise FileNotFoundError(f"Launch script not found: {script_path}")

        # Prepare environment and launch subprocess
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        log_dir = state_root() / "services"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{service_id}.log"

        cmd = [sys.executable, str(script_path), "--no-browser"]
        logger.info(f"Launching service '{service_id}' with command: {' '.join(cmd)}")

        with open(log_file, "a", encoding="utf-8") as out:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                env=env,
                stdout=out,
                stderr=out,
                creationflags=creationflags,
            )

        # Wait for service to come online
        start_wait = time.perf_counter()
        is_online = False
        resolved_url = service.url
        while (time.perf_counter() - start_wait) < max_wait_sec:
            poll_val = proc.poll() if hasattr(proc, "poll") and callable(proc.poll) else None
            if poll_val is not None and isinstance(poll_val, int):
                logger.error(f"Service '{service_id}' process exited prematurely (exit code: {poll_val})")
                break
            time.sleep(poll_interval)
            probe = self._probe_service_urls(service, timeout_sec=0.5)
            if probe.status == HealthStatus.ONLINE:
                is_online = True
                resolved_url = probe.url or service.url
                break

        if is_online:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=resolved_url,
                status="online",
                launched=True,
                message=f"Service '{service.name}' successfully launched (PID: {proc.pid}).",
            )
        else:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=resolved_url,
                status="starting",
                launched=True,
                message=f"Service '{service.name}' process spawned (PID: {proc.pid}), still warming up.",
            )

    def get_service_launch_target(self, service_id: str, max_wait_sec: float = 5.0) -> str:
        """
        Ensures local service is launched if applicable and returns its destination URL.
        """
        service = self.get_service(service_id)
        if not service:
            raise KeyError(f"Service '{service_id}' not found in catalog")

        if not self._local_service_launch_enabled():
            return service.url

        if service.launch_script:
            res = self.launch_service(service_id, max_wait_sec=max_wait_sec)
            if isinstance(res, ServiceLaunchResponse) and res.url:
                return res.url

        return service.url

    def export_services_data(self) -> ExportCatalogResponse:
        """Exports full catalog of services with timestamp and version."""
        from core.enterprise.enforcement import enforce_data_policy

        enforce_data_policy("darkfac", "export")
        services = self.list_services()
        now_iso = datetime.now(timezone.utc).isoformat()
        return ExportCatalogResponse(
            exported_at=now_iso,
            version="1.0",
            count=len(services),
            services=services,
        )

    def import_services_data(self, req: ImportCatalogRequest) -> ImportCatalogResponse:
        """Imports and saves catalog with automatic backup."""
        # Create backup of current file
        if self.services_file.exists():
            backup_file = self.services_file.with_suffix(".backup.json")
            try:
                backup_file.write_text(self.services_file.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(f"Created catalog backup at {backup_file}")
            except Exception as exc:
                logger.warning(f"Failed to create backup: {exc}")

        current_items = self._load_services_raw()
        if req.merge:
            existing_map = {item.get("id"): item for item in current_items}
            for new_service in req.services:
                existing_map[new_service.id] = new_service.model_dump()
            final_items = list(existing_map.values())
        else:
            final_items = [s.model_dump() for s in req.services]

        self._save_services_raw(final_items)
        return ImportCatalogResponse(
            imported_count=len(req.services),
            total_count=len(final_items),
            message="Catálogo importado com sucesso!",
        )

    def reset_to_defaults(self) -> int:
        """Restores catalog from default_services.json."""
        if not self.default_services_file.exists():
            raise FileNotFoundError("Arquivo default_services.json não encontrado.")

        content = self.default_services_file.read_text(encoding="utf-8")
        self.services_file.write_text(content, encoding="utf-8")
        items = json.loads(content)
        return len(items) if isinstance(items, list) else 0
