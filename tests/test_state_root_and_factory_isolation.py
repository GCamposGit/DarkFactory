"""Tests for state_root() resolution, runtime isolation, and static scanner (USR-88).

Ensures:
1. state_root() is dynamically resolved and respects DARKFAC_STATE_ROOT.
2. Notifications, usage ledger, telemetry, and canary reports use state_root().
3. Tree-hygiene guard detects creations and modifications under .factory/.
4. Static AST/regex scanner ensures no unapproved hardcoded .factory/ paths in core/.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from core.paths import project_root, state_root
from tests._tree_hygiene import diff_fingerprints, take_fingerprint


def test_state_root_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Default fallback: project_root() / ".factory"
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    assert state_root() == project_root() / ".factory"

    # 2. Configured override
    custom_root = tmp_path / "custom_state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(custom_root))
    assert state_root() == custom_root.resolve()


def test_core_components_use_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom_root = tmp_path / "sandbox_state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(custom_root))

    # NotificationStore
    from core.notifications.store import NotificationStore
    notif_store = NotificationStore()
    assert notif_store.store_path == custom_root / "notifications" / "notifications.jsonl"

    # ModelUsageLedger
    from core.usage.ledger import ModelUsageLedger
    usage_ledger = ModelUsageLedger()
    assert usage_ledger.storage_dir == custom_root / "usage"

    # TelemetryStore
    from core.telemetry.store import TelemetryStore, default_db_path
    assert default_db_path() == custom_root / "telemetry.db"
    tel_store = TelemetryStore()
    assert Path(tel_store.db_path) == custom_root / "telemetry.db"

    # Canary reports dir
    from core.line.canary import default_canary_reports_dir
    assert default_canary_reports_dir() == custom_root / "reports" / "canary"

    # Quota reservations
    from core.usage.reservation import QuotaReservationManager
    res_mgr = QuotaReservationManager()
    assert res_mgr.path == custom_root / "usage" / "reservations.json"

    # Evolution engine storage_dir
    from core.evolution.engine import FactoryEvolutionEngine
    evo_engine = FactoryEvolutionEngine(root=tmp_path / "fake_proj")
    assert evo_engine.storage_dir == custom_root / "evolution"


def test_tree_hygiene_detects_factory_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that tree-hygiene fingerprint catches unversioned mutations under .factory/."""
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    factory_dir = fake_repo / ".factory"
    factory_dir.mkdir()

    # Stub git status to return empty (clean git tree)
    monkeypatch.setattr(
        "tests._tree_hygiene.subprocess.run",
        lambda *args, **kwargs: type("Proc", (), {"returncode": 0, "stdout": ""})(),
    )

    fp_before = take_fingerprint(fake_repo)
    assert fp_before is not None
    assert len(fp_before) == 0

    # Create a new file in .factory
    sample_file = factory_dir / "notifications" / "test.jsonl"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text('{"event": "test"}\n', encoding="utf-8")

    fp_after = take_fingerprint(fake_repo)
    assert fp_after is not None
    rel_key = ".factory/notifications/test.jsonl"
    assert rel_key in fp_after
    assert fp_after[rel_key][0] == "!!"

    violations = diff_fingerprints(fp_before, fp_after)
    assert len(violations) == 1
    assert violations[0].kind == "new"
    assert violations[0].status == "!!"
    assert violations[0].path == rel_key

    # Modify file
    sample_file.write_text('{"event": "test2", "extra": 123}\n', encoding="utf-8")
    fp_modified = take_fingerprint(fake_repo)
    assert fp_modified is not None
    violations_mod = diff_fingerprints(fp_after, fp_modified)
    assert len(violations_mod) == 1
    assert violations_mod[0].kind == "changed"


# Allowlist for files permitted to reference .factory strings for versioned configs,
# governance rules, or path resolution definitions.
STATIC_SCAN_ALLOWLIST = {
    # Path definition itself
    "core/paths.py",
    # Guardrails and CI audits
    "core/orchestrator/guard.py",
    "core/ci_checks.py",
    "core/state_guard.py",
    "core/evolution/sandbox.py",
    "core/git/secret_scan.py",
    # Tracked/versioned static contracts
    "core/projects/registry.py",  # .factory/projects.json
    "core/demands/store.py",      # .factory/demands/demands.json
    "core/line/routing.py",       # .factory/line_routing.json (versioned contract)
}

DISALLOWED_FACTORY_SUBDIRS = {
    "notifications",
    "usage",
    "telemetry",
    "reports",
    "catalog",
    "content",
    "visuals",
    "evolution",
    "enterprise",
    "learning",
    "learning_packs",
    "marketing",
    "portfolio",
    "workflow",
    "telegram",
}


def test_no_hardcoded_factory_state_writers_in_core() -> None:
    """AST / string scan across core/**/*.py ensuring mutable state is not hardcoded to .factory.

    Any reference to mutable factory directories (usage, notifications, telemetry, etc.)
    must pass through state_root() rather than hardcoded Path(".factory/...") strings.
    """
    repo_root = project_root()
    core_dir = repo_root / "core"

    violations: list[str] = []

    pattern = re.compile(r"['\"]\.factory[/\\]([a-zA-Z0-9_\-]+)")

    for py_file in core_dir.glob("**/*.py"):
        rel_path = py_file.relative_to(repo_root).as_posix()
        if rel_path in STATIC_SCAN_ALLOWLIST:
            continue

        text = py_file.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            subdir = match.group(1)
            if subdir in DISALLOWED_FACTORY_SUBDIRS:
                line_no = text[: match.start()].count("\n") + 1
                violations.append(f"{rel_path}:{line_no} matches hardcoded '.factory/{subdir}'")

    assert not violations, (
        f"Found hardcoded mutable .factory paths in core/ (must use state_root()):\n"
        + "\n".join(violations)
    )
