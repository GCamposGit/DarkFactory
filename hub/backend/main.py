"""
Main FastAPI application entrypoint for DarkHub.
"""

import os
import re
import secrets
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from hub.backend.api import roadmap_router, router as api_router

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
SESSION_COOKIE = "darkhub_session"
SESSION_HEADER = "x-darkhub-session"
CONFIGURED_SESSION_TOKEN = os.environ.get("DARKHUB_SESSION_TOKEN")
SESSION_TOKEN = CONFIGURED_SESSION_TOKEN or secrets.token_urlsafe(32)


def _csv_environment(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    configured = os.environ.get(name)
    if not configured:
        return default
    values = tuple(value.strip() for value in configured.split(",") if value.strip())
    return values or default


ALLOWED_HOSTS = _csv_environment(
    "DARKHUB_ALLOWED_HOSTS",
    ("localhost", "127.0.0.1", "[::1]", "testserver"),
)
ALLOWED_ORIGINS = _csv_environment("DARKHUB_ALLOWED_ORIGINS", ())
_LOOPBACK_ORIGIN = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::(?:[1-9][0-9]{0,4}))?$",
    re.IGNORECASE,
)


def _origin_is_allowed(origin: str) -> bool:
    if ALLOWED_ORIGINS:
        return origin in ALLOWED_ORIGINS
    if _LOOPBACK_ORIGIN.fullmatch(origin) is None:
        return False
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.port is None or 1 <= parsed.port <= 65535


if ALLOWED_ORIGINS:
    cors_origin_regex = "^(?:" + "|".join(re.escape(origin) for origin in ALLOWED_ORIGINS) + ")$"
else:
    cors_origin_regex = _LOOPBACK_ORIGIN.pattern

app = FastAPI(
    title="DarkHub - AI & Dev Command Center",
    version="1.0.0",
    description="Cockpit centralizado para ferramentas de IA e desenvolvimento com monitoramento ativo e inferência local.",
)
app.state.session_token = SESSION_TOKEN


@app.middleware("http")
async def enforce_local_browser_boundary(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Reject hostile browser origins and invalid local-session credentials."""
    protected_path = request.url.path.startswith(("/api", "/projects"))
    if protected_path:
        origin = request.headers.get("origin")
        if origin and not _origin_is_allowed(origin):
            return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})

        supplied_session = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE)
        if supplied_session and not secrets.compare_digest(supplied_session, SESSION_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Invalid DarkHub session"})
        session_required = bool(origin) or CONFIGURED_SESSION_TOKEN is not None
        if session_required and request.method != "OPTIONS" and not supplied_session:
            return JSONResponse(status_code=401, content={"detail": "DarkHub session is required"})

    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))

# Include API Router
app.include_router(api_router)
app.include_router(roadmap_router, prefix="/api")
app.include_router(roadmap_router)


# Root route serving index.html
@app.get("/", response_class=FileResponse)
def serve_index() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    response = FileResponse(index_file)
    response.set_cookie(
        SESSION_COOKIE,
        SESSION_TOKEN,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("DARKHUB_SECURE_COOKIE", "0") == "1",
    )
    return response


# Mount static assets (CSS, JS)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

VISUALS_DIR = BASE_DIR.parent / ".factory" / "visuals"
VISUALS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/visuals", StaticFiles(directory=str(VISUALS_DIR)), name="visuals")
