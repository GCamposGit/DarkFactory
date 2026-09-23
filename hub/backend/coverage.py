"""DarkHub coverage gate: every factory capability must be reflected in the Hub (USR-42).

The DarkHub is the owner's primary interface. This module compares what the
factory exposes (``/api`` routes in the OpenAPI schema and ``core/`` packages)
with ``hub/coverage.json``. Each capability must be either wired to a Hub
surface, explicitly waived (machine-to-machine route or internal library), or
tracked as pending with a roadmap id from ``docs/DARKHUB_ROADMAP.md``.

``tests/test_hub_coverage.py`` runs this gate in the CI suite, so a PR that
adds a route or core module without reflecting it in the Hub fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "hub" / "coverage.json"
ROADMAP_PATH = REPO_ROOT / "docs" / "DARKHUB_ROADMAP.md"
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
CORE_DIR = REPO_ROOT / "core"

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
MIN_REASON_LENGTH = 12
_ROADMAP_ID = re.compile(r"\bDH-\d{2,3}\b")
_PATH_PARAM = re.compile(r"\{[^}]+\}")
_DOM_ID = re.compile(r"""\bid\s*=\s*["']([A-Za-z][\w-]*)["']""")


class CoverageEntry(BaseModel):
    """How one capability is reflected in the Hub. Exactly one field must be set."""

    model_config = ConfigDict(extra="forbid")

    surface: str | None = Field(default=None, description="DOM id of the Hub surface that exposes it")
    machine: str | None = Field(default=None, description="Why this route is machine-to-machine only")
    internal: str | None = Field(default=None, description="Why this core package has no owner-facing state")
    pending: str | None = Field(default=None, description="Roadmap id (DH-xx) that will add the surface")

    @model_validator(mode="after")
    def _exactly_one(self) -> "CoverageEntry":
        chosen = [name for name in ("surface", "machine", "internal", "pending") if getattr(self, name)]
        if len(chosen) != 1:
            raise ValueError(f"exactly one of surface/machine/internal/pending is required, got {chosen or 'none'}")
        return self


class CoverageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    description: str = ""
    api_routes: dict[str, CoverageEntry] = Field(default_factory=dict)
    core_modules: dict[str, CoverageEntry] = Field(default_factory=dict)


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problems: list[str] = Field(default_factory=list)
    surfaced: int = 0
    waived: int = 0
    pending: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def coverage_ratio(self) -> float:
        owner_facing = self.surfaced + self.pending
        return 1.0 if owner_facing == 0 else self.surfaced / owner_facing


def load_manifest(path: Path = MANIFEST_PATH) -> CoverageManifest:
    return CoverageManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def api_route_keys(openapi_schema: dict[str, Any]) -> set[str]:
    """``"METHOD /api/path"`` for every public (in-schema) Hub API operation."""
    keys: set[str] = set()
    for path, operations in openapi_schema.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue  # root aliases mirror /api routes and are not separate capabilities
        keys.update(f"{method.upper()} {path}" for method in operations if method in HTTP_METHODS)
    return keys


def core_module_names(core_dir: Path = CORE_DIR) -> set[str]:
    names: set[str] = set()
    for child in core_dir.iterdir():
        if child.name.startswith(("_", ".")):
            continue
        if child.is_dir() and any(child.glob("*.py")):
            names.add(child.name)  # regular and namespace packages alike
        elif child.is_file() and child.suffix == ".py":
            names.add(child.stem)
    return names


def frontend_text(frontend_dir: Path = FRONTEND_DIR) -> str:
    chunks = [path.read_text(encoding="utf-8") for path in sorted(frontend_dir.glob("*.js"))]
    chunks.append((frontend_dir / "index.html").read_text(encoding="utf-8"))
    return "\n".join(chunks).replace("${API_BASE}", "/api")


def frontend_ids(frontend_dir: Path = FRONTEND_DIR) -> set[str]:
    """Static ids from index.html plus ids of sections injected by the frontend scripts."""
    ids: set[str] = set()
    for source in [frontend_dir / "index.html", *sorted(frontend_dir.glob("*.js"))]:
        ids.update(_DOM_ID.findall(source.read_text(encoding="utf-8")))
    return ids


def route_is_called(route_key: str, text: str) -> bool:
    """Heuristic: every static chunk of the route path appears in the frontend code."""
    path = route_key.split(" ", 1)[1]
    chunks = [chunk for chunk in _PATH_PARAM.split(path) if chunk.strip("/")]
    return all(chunk.rstrip("/") in text for chunk in chunks)


def roadmap_ids(path: Path = ROADMAP_PATH) -> set[str]:
    return set(_ROADMAP_ID.findall(path.read_text(encoding="utf-8"))) if path.exists() else set()


def _check_entries(
    kind: str,
    actual: set[str],
    declared: dict[str, CoverageEntry],
    *,
    ids: set[str],
    roadmap: set[str],
    report: CoverageReport,
    called: Any = None,
) -> None:
    for key in sorted(actual - set(declared)):
        report.problems.append(
            f"[{kind}] '{key}' is not reflected in the DarkHub. Add it to hub/coverage.json "
            f"with a 'surface' (Hub DOM id), a waiver or 'pending': 'DH-xx' from docs/DARKHUB_ROADMAP.md."
        )
    for key in sorted(set(declared) - actual):
        report.problems.append(f"[{kind}] '{key}' is declared in hub/coverage.json but no longer exists; remove it.")
    for key in sorted(actual & set(declared)):
        entry = declared[key]
        if entry.surface:
            report.surfaced += 1
            if entry.surface not in ids:
                report.problems.append(f"[{kind}] '{key}' points to surface '{entry.surface}', not found in hub/frontend.")
            elif called is not None and not called(key):
                report.problems.append(f"[{kind}] '{key}' claims surface '{entry.surface}' but no frontend code calls it.")
        elif entry.pending:
            report.pending += 1
            if entry.pending not in roadmap:
                report.problems.append(f"[{kind}] '{key}' is pending on '{entry.pending}', absent from docs/DARKHUB_ROADMAP.md.")
        else:
            report.waived += 1
            reason = entry.machine or entry.internal or ""
            if len(reason) < MIN_REASON_LENGTH:
                report.problems.append(f"[{kind}] '{key}' waiver needs a real justification (>= {MIN_REASON_LENGTH} chars).")
            if kind == "core" and entry.machine:
                report.problems.append(f"[core] '{key}' must use 'internal', not 'machine'.")
            if kind == "api" and entry.internal:
                report.problems.append(f"[api] '{key}' must use 'machine', not 'internal'.")


def evaluate(
    manifest: CoverageManifest,
    *,
    routes: Iterable[str],
    modules: Iterable[str],
    ids: set[str],
    text: str,
    roadmap: set[str],
) -> CoverageReport:
    report = CoverageReport()
    _check_entries(
        "api",
        set(routes),
        manifest.api_routes,
        ids=ids,
        roadmap=roadmap,
        report=report,
        called=lambda key: route_is_called(key, text),
    )
    _check_entries("core", set(modules), manifest.core_modules, ids=ids, roadmap=roadmap, report=report)
    return report


def evaluate_repository(manifest_path: Path = MANIFEST_PATH) -> CoverageReport:
    from hub.backend.main import app

    return evaluate(
        load_manifest(manifest_path),
        routes=api_route_keys(app.openapi()),
        modules=core_module_names(),
        ids=frontend_ids(),
        text=frontend_text(),
        roadmap=roadmap_ids(),
    )
