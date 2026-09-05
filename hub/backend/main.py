"""
Main FastAPI application entrypoint for DarkHub.
"""

from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hub.backend.api import router as api_router

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(
    title="DarkHub - AI & Dev Command Center",
    version="1.0.0",
    description="Cockpit centralizado para ferramentas de IA e desenvolvimento com monitoramento ativo e inferência local.",
)

# Allow CORS for development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Router
app.include_router(api_router)


# Root route serving index.html
@app.get("/", response_class=FileResponse)
def serve_index() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return FileResponse(FRONTEND_DIR / "index.html")
    return FileResponse(index_file)


# Mount static assets (CSS, JS)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

VISUALS_DIR = BASE_DIR.parent / ".factory" / "visuals"
VISUALS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/visuals", StaticFiles(directory=str(VISUALS_DIR)), name="visuals")

