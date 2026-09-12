"""Autonomous n8n Community Self-Hosted Manager, Manifest Generator, and Sanitizer (HF-14).

Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 5, 8, 9, 10, lines 266, 299, 303)
and HYBRID_AUTONOMY_REQUIREMENTS (Scenarios G1, G5, G8).

Key Invariants:
1. Community Edition Compliance: $0 additional license target using n8nio/n8n official image.
2. Instance Preflight Verification: Distinguishes generic public website link (https://n8n.io)
   from real operational self-hosted instance with /healthz verification.
3. Isolated Persistence: Docker Compose manifest includes dedicated PostgreSQL 16 database and named volumes.
4. Workflow Sanitization: Exporter completely redacts node credentials and secret tokens before Git versioning.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("darkfac.integrations.n8n")

GENERIC_N8N_DOMAINS = {"n8n.io", "www.n8n.io", "https://n8n.io", "http://n8n.io"}


class N8nConfig(BaseModel):
    """Configuration options for n8n Community integration."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:5678"
    webhook_url: Optional[str] = None
    api_key: Optional[str] = None
    encryption_key: Optional[str] = None


def _normalise_origin(url: str) -> Optional[tuple[str, str, int]]:
    """Return a comparable HTTP origin, rejecting URLs carrying userinfo."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if scheme not in {"http", "https"} or not hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
    except (AttributeError, ValueError):
        return None

    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _is_webhook_path(path: str) -> bool:
    """Allow only n8n's public webhook routes, never arbitrary API/admin paths."""
    decoded_path = urllib.parse.unquote(path or "")
    segments = [segment for segment in decoded_path.split("/") if segment]
    if any(segment in {".", ".."} or "\\" in segment for segment in segments):
        return False
    return any(
        segment.lower() in {"webhook", "webhook-test"}
        and index + 1 < len(segments)
        and bool(segments[index + 1])
        for index, segment in enumerate(segments)
    )


class N8nInstanceReport(BaseModel):
    """Report produced by probing an n8n endpoint."""

    model_config = ConfigDict(extra="ignore")

    url: str
    is_generic_placeholder: bool = False
    operational: bool = False
    status_code: Optional[int] = None
    version: Optional[str] = None
    db_connected: bool = False
    error: Optional[str] = None


class N8nProbe:
    """Probes n8n endpoints to verify live health and detect generic placeholders."""

    def __init__(
        self,
        config: Optional[N8nConfig] = None,
        http_client: Optional[Callable[[str, float], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config or N8nConfig()
        self._http_client = http_client

    def probe(self, target_url: Optional[str] = None, timeout: float = 3.0) -> N8nInstanceReport:
        """Inspect the target URL. Rejects generic documentation links, queries /healthz on real hosts."""
        url = (target_url or self.config.base_url).strip()
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower() or parsed.path.lower()

        # Check for generic link
        if domain in GENERIC_N8N_DOMAINS or url.rstrip("/") in GENERIC_N8N_DOMAINS:
            return N8nInstanceReport(
                url=url,
                is_generic_placeholder=True,
                operational=False,
                error="Configured URL is the public generic documentation link (https://n8n.io), not an operational instance.",
            )

        if self._http_client:
            try:
                res = self._http_client(url, timeout)
                return N8nInstanceReport.model_validate(res)
            except Exception:
                return N8nInstanceReport(
                    url=url,
                    is_generic_placeholder=False,
                    operational=False,
                    error="n8n health probe failed",
                )

        # Default live HTTP probe
        health_url = f"{url.rstrip('/')}/healthz"
        req = urllib.request.Request(
            health_url,
            headers={"User-Agent": "DarkFac-Probe/1.0", "Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.status
                body_bytes = resp.read()
                version = resp.headers.get("X-N8N-Version")
                db_ok = True
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                    if isinstance(payload, dict):
                        version = payload.get("version", version)
                        status_str = payload.get("status", "ok")
                        db_ok = status_str.lower() in ("ok", "healthy")
                except Exception:
                    pass

                return N8nInstanceReport(
                    url=url,
                    is_generic_placeholder=False,
                    operational=status_code == 200,
                    status_code=status_code,
                    version=version or "community",
                    db_connected=db_ok,
                )
        except urllib.error.HTTPError as exc:
            return N8nInstanceReport(
                url=url,
                is_generic_placeholder=False,
                operational=False,
                status_code=exc.code,
                error=f"HTTP Error {exc.code}",
            )
        except Exception:
            return N8nInstanceReport(
                url=url,
                is_generic_placeholder=False,
                operational=False,
                error="n8n health probe connection failed",
            )


class N8nManifestGenerator:
    """Generates standard Docker Compose manifests for self-hosted n8n Community with dedicated PostgreSQL."""

    @staticmethod
    def generate_docker_compose(
        n8n_version: str = "latest",
        postgres_version: str = "16-alpine",
        port: int = 5678,
        webhook_url: str = "http://localhost:5678",
        encryption_key_placeholder: str = "CHANGE_ME_N8N_ENCRYPTION_KEY_SECRET",
    ) -> str:
        """Render a production-ready docker-compose.yml for Dokploy or standalone Docker."""
        compose_content = f"""# ==============================================================================
# Dark Factory: n8n Community Self-Hosted Manifest (HF-14)
# Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 10)
# ==============================================================================
version: "3.8"

services:
  n8n-postgres:
    image: postgres:{postgres_version}
    container_name: darkfac-n8n-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: n8n
      POSTGRES_PASSWORD: ${{N8N_DB_PASSWORD:-darkfac_n8n_secure_pass}}
      POSTGRES_DB: n8n
    volumes:
      - n8n_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U n8n"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - darkfac_n8n_net

  n8n:
    image: n8nio/n8n:{n8n_version}
    container_name: darkfac-n8n-app
    restart: unless-stopped
    ports:
      - "{port}:5678"
    environment:
      - N8N_PORT=5678
      - N8N_PROTOCOL=http
      - WEBHOOK_URL={webhook_url}
      - N8N_ENCRYPTION_KEY=${{N8N_ENCRYPTION_KEY:-{encryption_key_placeholder}}}
      - DB_TYPE=postgresdb
      - DB_POSTGRESDB_HOST=n8n-postgres
      - DB_POSTGRESDB_PORT=5432
      - DB_POSTGRESDB_DATABASE=n8n
      - DB_POSTGRESDB_USER=n8n
      - DB_POSTGRESDB_PASSWORD=${{N8N_DB_PASSWORD:-darkfac_n8n_secure_pass}}
      - EXECUTIONS_DATA_PRUNE=true
      - EXECUTIONS_DATA_MAX_AGE=336 # 14 days retention
    volumes:
      - n8n_app_data:/home/node/.n8n
    depends_on:
      n8n-postgres:
        condition: service_healthy
    networks:
      - darkfac_n8n_net

volumes:
  n8n_postgres_data:
  n8n_app_data:

networks:
  darkfac_n8n_net:
    driver: bridge
"""
        return compose_content


class N8nWorkflowManager:
    """Manages sanitized export and validation of n8n workflows."""

    @staticmethod
    def export_workflow(workflow_data: Dict[str, Any], output_path: Optional[Path] = None) -> Dict[str, Any]:
        """Strip all sensitive credentials, passwords, and tokens before committing workflow JSON."""
        sanitized = json.loads(json.dumps(workflow_data))  # deep copy

        nodes = sanitized.get("nodes", [])
        for node in nodes:
            # 1. Clear credentials object
            if "credentials" in node and isinstance(node["credentials"], dict):
                node["credentials"] = {k: {"id": "REDACTED", "name": "REDACTED"} for k in node["credentials"]}

            # 2. Scrub sensitive parameters (tokens, passwords)
            params = node.get("parameters", {})
            if isinstance(params, dict):
                for key in list(params.keys()):
                    key_lower = key.lower()
                    if any(s in key_lower for s in ("token", "secret", "password", "apikey", "api_key", "authorization")):
                        params[key] = "[REDACTED_SECRET]"

        # 3. Clear pinData (test execution payloads that may hold secrets)
        if "pinData" in sanitized:
            sanitized["pinData"] = {}

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")

        return sanitized

    @staticmethod
    def import_workflow(workflow_data: Dict[str, Any]) -> bool:
        """Validate an incoming workflow definition for compatibility with Community edition."""
        if not isinstance(workflow_data, dict):
            return False

        nodes = workflow_data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) == 0:
            return False

        # Verify all nodes have 'name' and 'type'
        for node in nodes:
            if not isinstance(node, dict) or "name" not in node or "type" not in node:
                return False

        # Verify connections structure
        connections = workflow_data.get("connections")
        if connections is not None and not isinstance(connections, dict):
            return False

        return True


class N8nApiResult(BaseModel):
    """Result of an API or Webhook call to n8n."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    status_code: Optional[int] = None
    data: Optional[Any] = None
    error: Optional[str] = None


def load_n8n_config(config_file: Optional[Path] = None) -> N8nConfig:
    """Loads N8nConfig from .factory/n8n/config.json or environment variables."""
    cfg_path = config_file or (Path(__file__).resolve().parents[2] / ".factory" / "n8n" / "config.json")
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return N8nConfig.model_validate(data)
        except Exception:
            pass
    return N8nConfig(
        base_url=os.environ.get("N8N_URL", "https://n8n.ggcampos.com"),
        api_key=os.environ.get("N8N_API_KEY"),
        webhook_url=os.environ.get("N8N_WEBHOOK_URL"),
        encryption_key=os.environ.get("N8N_ENCRYPTION_KEY"),
    )


class N8nApiClient:
    """Autonomous API Client for DarkFac agents interacting with n8n Community.

    Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 10).
    Provides programmatic CRUD for workflows, webhook dispatch, and execution telemetry.
    """

    def __init__(
        self,
        config: Optional[N8nConfig] = None,
        http_client: Optional[Callable[[str, str, Dict[str, str], Optional[bytes], float], Dict[str, Any]]] = None,
    ) -> None:
        self.config = config or load_n8n_config()
        self._http_client = http_client

    def _get_headers(self, *, include_api_key: bool = True) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DarkFac-Autonomous-Agent/1.0",
        }
        if include_api_key and self.config.api_key:
            headers["X-N8N-API-KEY"] = self.config.api_key
        return headers

    def _allowed_origins(self) -> set[tuple[str, str, int]]:
        """Build the n8n origin allowlist from explicitly configured endpoints."""
        configured_urls = [self.config.base_url]
        if self.config.webhook_url:
            configured_urls.append(self.config.webhook_url)
        return {
            origin
            for configured_url in configured_urls
            if (origin := _normalise_origin(configured_url)) is not None
        }

    def _resolve_allowed_url(self, path_or_url: str) -> Optional[str]:
        """Resolve a request URL and reject origins outside the configured n8n allowlist."""
        if not isinstance(path_or_url, str) or not path_or_url.strip():
            return None

        raw_url = path_or_url.strip()
        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except ValueError:
            return None

        if parsed.scheme:
            if parsed.scheme.lower() not in {"http", "https"}:
                return None
            candidate = raw_url
        else:
            # Treat all relative paths as paths on the configured API origin.
            base = self.config.base_url.rstrip("/")
            if not base or raw_url.startswith("//"):
                return None
            candidate = f"{base}/{raw_url.lstrip('/')}"

        try:
            candidate_parts = urllib.parse.urlsplit(candidate)
            if candidate_parts.fragment:
                return None
        except ValueError:
            return None

        origin = _normalise_origin(candidate)
        if origin is None or origin not in self._allowed_origins():
            return None
        return candidate

    def _request(
        self,
        method: str,
        path_or_url: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> N8nApiResult:
        full_url = self._resolve_allowed_url(path_or_url)
        if full_url is None:
            return N8nApiResult(
                success=False,
                status_code=None,
                error="n8n destination is outside the configured allowlist",
            )

        # _resolve_allowed_url guarantees that credentials can only be sent to
        # an explicitly configured n8n origin.
        headers = self._get_headers(include_api_key=True)
        body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None

        if self._http_client:
            try:
                res = self._http_client(method, full_url, headers, body_bytes, timeout)
                return N8nApiResult.model_validate(res)
            except Exception:
                return N8nApiResult(
                    success=False,
                    status_code=None,
                    error="n8n HTTP client request failed",
                )

        req = urllib.request.Request(full_url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
                data = None
                if raw:
                    try:
                        data = json.loads(raw.decode("utf-8"))
                    except Exception:
                        data = raw.decode("utf-8", errors="replace")
                return N8nApiResult(success=status < 400, status_code=status, data=data)
        except urllib.error.HTTPError as exc:
            return N8nApiResult(
                success=False,
                status_code=exc.code,
                error=f"n8n request returned HTTP {exc.code}",
            )
        except Exception:
            return N8nApiResult(
                success=False,
                status_code=None,
                error="n8n connection failed",
            )

    def get_health(self) -> N8nInstanceReport:
        """Probes the live /healthz endpoint of the configured instance."""
        probe = N8nProbe(config=self.config)
        return probe.probe(target_url=self.config.base_url)

    def list_workflows(self, limit: int = 50) -> N8nApiResult:
        """Lists registered workflows from n8n API."""
        return self._request("GET", f"/api/v1/workflows?limit={limit}")

    def get_workflow(self, workflow_id: str) -> N8nApiResult:
        """Fetches detailed workflow object by ID."""
        return self._request("GET", f"/api/v1/workflows/{workflow_id}")

    def create_workflow(
        self,
        name: str,
        nodes: List[Dict[str, Any]],
        connections: Dict[str, Any],
        settings: Optional[Dict[str, Any]] = None,
        active: bool = False,
    ) -> N8nApiResult:
        """Creates a new workflow via n8n API."""
        clean_nodes = []
        for n in nodes:
            node_copy = dict(n)
            if "credentials" in node_copy:
                creds = node_copy["credentials"]
                if isinstance(creds, dict) and any(
                    isinstance(v, dict) and v.get("id") == "REDACTED" for v in creds.values()
                ):
                    del node_copy["credentials"]
            clean_nodes.append(node_copy)

        payload = {
            "name": name,
            "nodes": clean_nodes,
            "connections": connections,
            "settings": settings or {},
        }
        res = self._request("POST", "/api/v1/workflows", payload=payload)
        if res.success and active and isinstance(res.data, dict) and "id" in res.data:
            self.activate_workflow(res.data["id"])
        return res

    def update_workflow(
        self,
        workflow_id: str,
        workflow_data: Dict[str, Any],
    ) -> N8nApiResult:
        """Updates an existing workflow by ID."""
        clean_data = dict(workflow_data)
        clean_data.pop("active", None)
        if "nodes" in clean_data and isinstance(clean_data["nodes"], list):
            clean_nodes = []
            for n in clean_data["nodes"]:
                node_copy = dict(n)
                if "credentials" in node_copy:
                    creds = node_copy["credentials"]
                    if isinstance(creds, dict) and any(
                        isinstance(v, dict) and v.get("id") == "REDACTED" for v in creds.values()
                    ):
                        del node_copy["credentials"]
                clean_nodes.append(node_copy)
            clean_data["nodes"] = clean_nodes
        return self._request("PUT", f"/api/v1/workflows/{workflow_id}", payload=clean_data)

    def activate_workflow(self, workflow_id: str) -> N8nApiResult:
        """Activates a workflow by ID."""
        return self._request("POST", f"/api/v1/workflows/{workflow_id}/activate")

    def deactivate_workflow(self, workflow_id: str) -> N8nApiResult:
        """Deactivates a workflow by ID."""
        return self._request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")

    def delete_workflow(self, workflow_id: str) -> N8nApiResult:
        """Deletes a workflow by ID."""
        return self._request("DELETE", f"/api/v1/workflows/{workflow_id}")

    def trigger_webhook(
        self,
        path_or_url: str,
        payload: Dict[str, Any],
        method: str = "POST",
        timeout: float = 15.0,
    ) -> N8nApiResult:
        """Triggers an n8n webhook workflow with JSON payload."""
        if not isinstance(path_or_url, str) or not path_or_url.strip():
            return N8nApiResult(success=False, error="n8n webhook path is empty")

        raw_path = path_or_url.strip()
        try:
            raw_parts = urllib.parse.urlsplit(raw_path)
        except ValueError:
            return N8nApiResult(success=False, error="n8n webhook URL is malformed")

        if raw_parts.scheme:
            # Full URLs are accepted only when their origin is configured below.
            url = raw_path
        else:
            if raw_path.startswith("//") or any(part == ".." for part in raw_path.split("/")):
                return N8nApiResult(success=False, error="n8n webhook path is invalid")
            base = (self.config.webhook_url or self.config.base_url).rstrip("/")
            url = f"{base}/{raw_path.lstrip('/')}"

        try:
            url_parts = urllib.parse.urlsplit(url)
        except ValueError:
            return N8nApiResult(success=False, error="n8n webhook URL is malformed")
        if not _is_webhook_path(url_parts.path):
            return N8nApiResult(success=False, error="n8n webhook path is not allowed")
        return self._request(method=method, path_or_url=url, payload=payload, timeout=timeout)

    def list_executions(self, workflow_id: Optional[str] = None, limit: int = 10) -> N8nApiResult:
        """Queries recent execution records and logs."""
        query = f"?limit={limit}"
        if workflow_id:
            query += f"&workflowId={workflow_id}"
        return self._request("GET", f"/api/v1/executions{query}")

    def sync_workflow_file(self, workflow_path: Path, activate: bool = True) -> N8nApiResult:
        """Loads a workflow JSON file, validates it, and synchronizes with n8n."""
        if not workflow_path.is_file():
            return N8nApiResult(success=False, error=f"File not found: {workflow_path}")

        try:
            content = workflow_path.read_text(encoding="utf-8")
            wf_data = json.loads(content)
        except Exception as exc:
            return N8nApiResult(success=False, error=f"Invalid JSON in {workflow_path}: {exc}")

        if not N8nWorkflowManager.import_workflow(wf_data):
            return N8nApiResult(success=False, error=f"Workflow validation failed for {workflow_path}")

        wf_name = wf_data.get("name") or workflow_path.stem

        # Check if already exists
        list_res = self.list_workflows(limit=100)
        existing_id = None
        if list_res.success and isinstance(list_res.data, dict) and "data" in list_res.data:
            for item in list_res.data["data"]:
                if item.get("name") == wf_name:
                    existing_id = item.get("id")
                    break

        if existing_id:
            update_payload = {
                "name": wf_name,
                "nodes": wf_data.get("nodes", []),
                "connections": wf_data.get("connections", {}),
                "settings": wf_data.get("settings", {}),
            }
            res = self.update_workflow(existing_id, update_payload)
            if res.success and activate:
                self.activate_workflow(existing_id)
            return res
        else:
            res = self.create_workflow(
                name=wf_name,
                nodes=wf_data.get("nodes", []),
                connections=wf_data.get("connections", {}),
                settings=wf_data.get("settings", {}),
                active=activate,
            )
            return res

    def sync_all_workflows(self, workflows_dir: Path, activate: bool = True) -> Dict[str, N8nApiResult]:
        """Scans a directory of workflow JSONs and synchronizes all into n8n."""
        results: Dict[str, N8nApiResult] = {}
        if not workflows_dir.exists():
            return results

        resolved_root = workflows_dir.resolve()
        for json_file in sorted(workflows_dir.glob("*.json")):
            try:
                resolved_file = json_file.resolve()
                resolved_file.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                results[json_file.name] = N8nApiResult(
                    success=False,
                    error="workflow file resolves outside the n8n workflows directory",
                )
                continue
            results[json_file.name] = self.sync_workflow_file(resolved_file, activate=activate)
        return results
