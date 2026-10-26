"""Guard the USR-385 function-size refactors."""

from __future__ import annotations

import ast
import json
from pathlib import Path


def _oversized_functions(source_path: Path) -> list[str]:
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    return [
        f"{node.name}: {node.end_lineno - node.lineno + 1} lines"
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno - node.lineno + 1 > 150
    ]


def _baseline_functions() -> dict[str, int]:
    repo_root = Path(__file__).parents[1]
    baseline = json.loads(
        (repo_root / ".factory" / "config" / "complexity_baseline.json").read_text(encoding="utf-8")
    )
    return baseline["functions"]


def test_readiness_functions_are_at_most_150_lines() -> None:
    source_path = Path(__file__).parents[1] / "core" / "workflow" / "readiness.py"
    assert not _oversized_functions(source_path)


def test_readiness_evaluate_is_removed_from_complexity_baseline() -> None:
    assert "core/workflow/readiness.py::ReadinessGate.evaluate" not in _baseline_functions()


def test_telegram_functions_are_at_most_150_lines() -> None:
    source_path = Path(__file__).parents[1] / "core" / "integrations" / "telegram.py"
    assert not _oversized_functions(source_path)


def test_autonomy_functions_are_at_most_150_lines() -> None:
    source_path = Path(__file__).parents[1] / "core" / "git" / "autonomy.py"
    assert not _oversized_functions(source_path)


def test_telegram_handle_message_is_removed_from_complexity_baseline() -> None:
    assert "core/integrations/telegram.py::TelegramGateway._handle_message" not in _baseline_functions()


def test_autonomy_deliver_is_removed_from_complexity_baseline() -> None:
    assert "core/git/autonomy.py::GitAutonomyManager._deliver" not in _baseline_functions()


def test_autonomy_complete_ticket_is_removed_from_complexity_baseline() -> None:
    assert "core/git/autonomy.py::GitAutonomyManager.complete_ticket" not in _baseline_functions()
