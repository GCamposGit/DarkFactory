"""Drift test for registry-generated docs (USR-185, slice 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models.docgen import REPO_ROOT, TARGETS, has_problems, process, render_text
from core.models.registry import get_registry, parse_registry

pytestmark = [pytest.mark.offline]

_RAW = {
    "models": [
        {"id": "m-a", "provider": "p", "family": "f1", "tier": "mid", "aliases": ["a"], "status": "active"},
        {"id": "m-b", "provider": "q", "family": "f2", "tier": "low", "status": "active", "price_input_per_mtok": 1.5},
    ],
    "roles": {"development.x": "m-a", "review.y": "m-b"},
    "review_pairs": [{"implementer": "development.x", "reviewer": "review.y"}],
}


def test_repo_docs_match_registry() -> None:
    results = process(REPO_ROOT, get_registry())
    assert not has_problems(results), [r for r in results if r.changed or r.missing_sections or r.unknown_sections]


def test_targets_exist_and_are_not_protected() -> None:
    protected = {"AGENTS.md", "CLAUDE.md", "FACTORY_RULES.md", "MISSION.md"}
    for rel in TARGETS:
        assert (REPO_ROOT / rel).is_file()
        assert rel not in protected and not rel.startswith(("core/harness/", ".github/"))


def test_idempotent_and_detects_drift(tmp_path: Path) -> None:
    reg = parse_registry(_RAW)
    doc = tmp_path / "d.md"
    doc.write_text("intro\n<!-- BEGIN model-registry:roles -->\nstale\n<!-- END model-registry:roles -->\ntail\n", encoding="utf-8")
    targets = {"d.md": ("roles",)}
    assert has_problems(process(tmp_path, reg, targets))  # check mode: diverges
    assert doc.read_text(encoding="utf-8").count("stale") == 1  # check did not write
    process(tmp_path, reg, targets, write=True)
    first = doc.read_text(encoding="utf-8")
    assert "`development.x` | `m-a`" in first and first.startswith("intro\n") and first.endswith("tail\n")
    assert not has_problems(process(tmp_path, reg, targets))
    process(tmp_path, reg, targets, write=True)
    assert doc.read_text(encoding="utf-8") == first


def test_missing_and_unknown_markers_flagged(tmp_path: Path) -> None:
    reg = parse_registry(_RAW)
    _, res = render_text("<!-- BEGIN model-registry:zzz -->\n<!-- END model-registry:zzz -->\n", reg, ("roles",))
    assert res.missing_sections == ("roles",) and res.unknown_sections == ("zzz",)
    assert has_problems(process(tmp_path, reg, {"absent.md": ("roles",)}))
