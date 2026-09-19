"""
Main FastAPI application entrypoint for DarkHub.
"""

from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import Depends, FastAPI, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import os
import urllib.parse
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.responses import Response, JSONResponse

from core.demands.models import DemandInput
from hub.backend.api import get_hub_service, roadmap_router, router as api_router
from hub.backend.models import ProgressProjection, TaskDashboardReport
from hub.backend.service import HubService

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

ALLOWED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "testserver",
}


def get_allowed_hosts() -> set[str]:
    """Returns the set of permitted hostnames, combining loopback defaults and DARKHUB_ALLOWED_HOSTS."""
    hosts = set(ALLOWED_HOSTS)
    env_hosts = os.getenv("DARKHUB_ALLOWED_HOSTS")
    if env_hosts:
        for h in env_hosts.split(","):
            cleaned = h.strip().lower()
            if cleaned:
                # Support host with optional port stripped
                if ":" in cleaned and not cleaned.startswith("["):
                    cleaned = cleaned.split(":")[0]
                hosts.add(cleaned)
    return hosts


def _extract_host(host_header: str) -> str:
    cleaned = host_header.strip()
    if cleaned.startswith("["):
        end = cleaned.find("]")
        if end != -1:
            return cleaned[1:end].lower()
    return cleaned.split(":")[0].lower()


class SecurityContainmentMiddleware:
    """
    Middleware enforcing Host, Origin, and Session containment boundaries (DF-08 & USR-18).
    Rejects foreign Host headers (preventing DNS rebinding).
    Rejects unauthorized Origin headers (preventing cross-origin attacks).
    Rejects invalid session tokens if sent.
    Enforces Cloudflare Access Zero Trust identity when CLOUDFLARE_ZERO_TRUST_REQUIRED is true.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            allowed = get_allowed_hosts()

            # 1. Host header validation
            host_header = headers.get(b"host", b"").decode("latin1").strip()
            if host_header:
                host_only = _extract_host(host_header)
                if host_only not in allowed:
                    response = Response("Invalid Host header", status_code=400)
                    await response(scope, receive, send)
                    return

            # 2. Origin header validation
            origin_header = headers.get(b"origin", b"").decode("latin1").strip()
            if origin_header:
                parsed_origin = urllib.parse.urlparse(origin_header)
                origin_host = (parsed_origin.hostname or "").strip("[]").lower()
                if origin_host not in allowed:
                    response = Response("Invalid Origin header", status_code=403)
                    await response(scope, receive, send)
                    return

            # 3. Cloudflare Access Zero Trust validation (if enabled)
            path = scope.get("path", "")
            cf_required = os.getenv("CLOUDFLARE_ZERO_TRUST_REQUIRED", "false").lower() == "true"
            # Webhook endpoints authenticate via HMAC signature rather than Cloudflare Access cookie
            is_webhook_path = path.startswith("/api/webhooks")
            if cf_required and not is_webhook_path:
                cf_user_email = headers.get(b"cf-access-authenticated-user-email", b"").decode("latin1").strip()
                if not cf_user_email:
                    response = JSONResponse(
                        {"detail": "Missing Cloudflare Access authentication header (Cf-Access-Authenticated-User-Email)"},
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return

            # 3. Session token validation (if provided, must be valid)
            session_header = headers.get(b"x-hub-session", b"").decode("latin1").strip()
            if session_header:
                from hub.backend.api import get_hub_service
                overrides = getattr(app, "dependency_overrides", {})
                override_fn = overrides.get(get_hub_service)
                service = override_fn() if override_fn else get_hub_service()
                if not service.validate_session(session_header):
                    response = JSONResponse(
                        {"detail": "Invalid session token"},
                        status_code=401,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)


from contextlib import asynccontextmanager

_logger_hub = __import__("logging").getLogger("dark_factory.hub")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    Hub startup: ensures the benchmark ledger is fresh before serving traffic.
    Runs in a background thread to avoid blocking the event loop.
    Failures are logged as warnings only — never block startup.
    """
    import asyncio
    try:
        from core.benchmarks.fetcher import ensure_daily_benchmark
        await asyncio.to_thread(ensure_daily_benchmark)
        _logger_hub.info("[STARTUP] Benchmark ledger refreshed on hub start.")
    except Exception as exc:
        _logger_hub.warning(f"[STARTUP] Benchmark refresh skipped at startup: {exc}")
    yield
    # Teardown (none needed)


app = FastAPI(
    title="DarkHub - AI & Dev Command Center",
    version="1.0.0",
    description="Cockpit centralizado para ferramentas de IA e desenvolvimento com monitoramento ativo e inferência local.",
    lifespan=_lifespan,
)


# Restrict CORS to trusted loopback origins
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\]|testserver)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security boundary middleware
app.add_middleware(SecurityContainmentMiddleware)

# Include API Router
app.include_router(api_router)
app.include_router(roadmap_router, prefix="/api")
app.include_router(roadmap_router)

# Root-level aliases for operational task dashboard (eliminating 404 on reverse proxy / direct calls)
@app.get("/tasks/dashboard", response_model=TaskDashboardReport, tags=["DarkHub Tasks"], include_in_schema=False)
@app.get("/tasks/dashboard/", response_model=TaskDashboardReport, include_in_schema=False)
@app.get("/tasks", response_model=TaskDashboardReport, include_in_schema=False)
@app.get("/tasks/", response_model=TaskDashboardReport, include_in_schema=False)
def get_task_dashboard_root(service: HubService = Depends(get_hub_service)) -> TaskDashboardReport:
    """Root alias for the operational task dashboard projection (DF-21 / HF-13)."""
    return service.get_task_dashboard()


# Root-level aliases for continuous progress projection (HF-13-02)
@app.get("/{project_id}/roadmap/progress", response_model=ProgressProjection, tags=["Operational Roadmap"], include_in_schema=False)
@app.get("/{project_id}/roadmap/progress/", response_model=ProgressProjection, include_in_schema=False)
def get_project_roadmap_progress_root(
    project_id: str,
    service: HubService = Depends(get_hub_service),
) -> ProgressProjection:
    """Root alias for the continuous progress and stagnation projection (HF-13-02)."""
    return service.get_progress_projection(project_id)


# Root-level aliases for autonomous intake (HF-08-02)
@app.post("/demands/intake", status_code=status.HTTP_202_ACCEPTED, tags=["DarkHub Demands"], include_in_schema=False)
@app.post("/demands/intake/", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def submit_autonomous_intake_root(
    demand: DemandInput,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_operator_token: Optional[str] = Header(None, alias="X-Operator-Token"),
    mode: str = Query("autonomous", pattern="^(autonomous|documentary)$"),
    policy_ref: str = Query("policy-v1"),
    service: HubService = Depends(get_hub_service),
) -> Dict[str, Any]:
    """Root alias for the autonomous transactional intake endpoint (HF-08-02)."""
    from hub.backend.api import submit_autonomous_intake
    return submit_autonomous_intake(
        demand=demand,
        idempotency_key=idempotency_key,
        x_operator_token=x_operator_token,
        mode=mode,
        policy_ref=policy_ref,
        service=service,
    )


# Root route serving index.html
@app.get("/", response_class=FileResponse)
@app.head("/", response_class=FileResponse)
def serve_index() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    return FileResponse(
        index_file,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# Mount static assets (CSS, JS)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

VISUALS_DIR = BASE_DIR.parent / ".factory" / "visuals"
VISUALS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/visuals", StaticFiles(directory=str(VISUALS_DIR)), name="visuals")
