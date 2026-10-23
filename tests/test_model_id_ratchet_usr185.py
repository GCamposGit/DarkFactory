"""Ratchet for literal model ids outside the allowlist (USR-185, slice 10).

The baseline in .factory/config/model_id_baseline.json may only go down. To lower it, migrate ids to aliases and
regenerate with: python scripts/model_id_census.py --write-baseline
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import model_id_census as census  # noqa: E402

BASELINE = REPO_ROOT / ".factory" / "config" / "model_id_baseline.json"
HINT = "Use an alias via core.models.registry (resolve/id_of/gateway_id), or mark a wire id with 'census: wire-id'."


def violations(current_total: int, current: dict[str, int], baseline: dict) -> list[str]:
    problems: list[str] = []
    base_files: dict[str, int] = baseline["files"]
    for rel, count in sorted(current.items()):
        allowed = base_files.get(rel, 0)
        if count > allowed:
            problems.append(f"{rel}: {count} literal model ids (baseline {allowed}). {HINT}")
    if current_total > baseline["total"]:
        problems.append(f"total {current_total} > baseline {baseline['total']}. {HINT}")
    return problems


def test_literal_model_ids_do_not_exceed_baseline() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    total, per_file = census.census()
    problems = violations(total, per_file, baseline)
    assert not problems, "\n".join(problems)


def test_ratchet_fails_for_planted_id(tmp_path: Path) -> None:
    planted = tmp_path / "new_module.py"
    planted.write_text('MODEL = "claude-fictional-9-9"\n', encoding="utf-8")
    count = census.count_ids(planted.read_text(encoding="utf-8"))
    assert count == 1
    problems = violations(10, {"core/new_module.py": count}, {"total": 9, "files": {}})
    assert any("core/new_module.py" in p and "core.models.registry" in p for p in problems)
    assert any(p.startswith("total") for p in problems)


def test_ratchet_allows_lower_or_equal() -> None:
    assert violations(5, {"a.py": 2}, {"total": 5, "files": {"a.py": 2}}) == []


def test_run_artifacts_are_excluded_from_model_id_census() -> None:
    assert census.is_allowlisted(".darkfac/runs/run-example/progress.json")
