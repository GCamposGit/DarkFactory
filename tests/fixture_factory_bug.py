"""Dependency-free real bug fixture used by the DF-15 acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path

PATCH_RESPONSE = json.dumps(
    {
        "path": "calculator.py",
        "old_text": "return left - right",
        "new_text": "return left + right",
        "rationale": "The add issue is caused by subtraction.",
    },
    separators=(",", ":"),
)


def create_factory_bug_fixture(workdir: Path, config: dict) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    (workdir / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_issue_acceptance() -> None:\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    protected_holdout = Path(__file__).resolve().parents[1] / ".factory" / "holdout" / "df15_factory_vertical.py"
    holdout_target = workdir / ".factory" / "holdout" / "df15_factory_vertical.py"
    holdout_target.parent.mkdir(parents=True, exist_ok=True)
    holdout_target.write_text(protected_holdout.read_text(encoding="utf-8"), encoding="utf-8")
    config_path = workdir / "factory_vertical.config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path
