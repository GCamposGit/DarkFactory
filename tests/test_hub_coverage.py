"""USR-42 gate: every factory capability is reflected in the DarkHub or tracked in its roadmap.

If this test fails on your PR, you added (or removed) an ``/api`` route or a
``core/`` package without updating ``hub/coverage.json``. Run
``python scripts/hub_coverage.py`` for the full diagnosis.
"""

from __future__ import annotations

from hub.backend.coverage import CoverageEntry, CoverageManifest, evaluate, evaluate_repository, route_is_called

import pytest


def test_repository_hub_coverage_gate() -> None:
    report = evaluate_repository()
    assert report.ok, "DarkHub coverage gate failed:\n" + "\n".join(report.problems)
    assert report.surfaced > 0


def _evaluate(manifest: dict, *, routes=(), modules=(), ids=frozenset({"panel"}), text="", roadmap=frozenset({"DH-01"})):
    return evaluate(
        CoverageManifest.model_validate(manifest),
        routes=routes,
        modules=modules,
        ids=set(ids),
        text=text,
        roadmap=set(roadmap),
    )


def test_new_route_or_module_without_entry_fails() -> None:
    report = _evaluate({}, routes={"GET /api/new/thing"}, modules={"brand_new"})
    assert len(report.problems) == 2
    assert "GET /api/new/thing" in report.problems[0]
    assert "brand_new" in report.problems[1]


def test_orphan_entries_fail() -> None:
    report = _evaluate({"api_routes": {"GET /api/gone": {"pending": "DH-01"}}})
    assert report.problems and "no longer exists" in report.problems[0]


def test_surface_must_exist_and_be_called() -> None:
    manifest = {"api_routes": {"GET /api/x/{id}": {"surface": "panel"}, "GET /api/y": {"surface": "ghost"}}}
    report = _evaluate(manifest, routes={"GET /api/x/{id}", "GET /api/y"}, text="fetch(`/api/x/${id}`)")
    assert report.problems == ["[api] 'GET /api/y' points to surface 'ghost', not found in hub/frontend."]

    uncalled = _evaluate({"api_routes": {"GET /api/z": {"surface": "panel"}}}, routes={"GET /api/z"})
    assert "no frontend code calls it" in uncalled.problems[0]


def test_pending_needs_known_roadmap_id_and_waivers_need_reasons() -> None:
    manifest = {
        "api_routes": {"POST /api/a": {"pending": "DH-99"}, "POST /api/b": {"machine": "hook"}},
        "core_modules": {"lib": {"machine": "wrong kind of waiver here"}},
    }
    report = _evaluate(manifest, routes={"POST /api/a", "POST /api/b"}, modules={"lib"})
    joined = "\n".join(report.problems)
    assert "DH-99" in joined
    assert "real justification" in joined
    assert "must use 'internal'" in joined


def test_entry_requires_exactly_one_status() -> None:
    with pytest.raises(ValueError):
        CoverageEntry(surface="panel", pending="DH-01")
    with pytest.raises(ValueError):
        CoverageEntry()


def test_route_heuristic_uses_every_static_chunk() -> None:
    assert route_is_called("GET /api/projects/{p}/roadmap/health", "`/api/projects/${p}/roadmap/health`")
    assert not route_is_called("GET /api/projects/{p}/roadmap/health", "`/api/projects/${p}/roadmap`")
