"""Static guard: only registered files may carry ``pytest.mark.serial`` (USR-263).

No test is run and no collection happens: the test files are parsed with ``ast``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._serial_marker_registry import SERIAL_ALLOWED

TESTS_DIR = Path(__file__).resolve().parent


def _uses_serial_marker(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "serial"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            return True
    return False


def serial_marked_files() -> set[str]:
    found: set[str] = set()
    for path in TESTS_DIR.rglob("*.py"):
        if path.name in {"test_serial_marker_guard.py", "_serial_marker_registry.py"}:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if _uses_serial_marker(tree):
            found.add(path.relative_to(TESTS_DIR).as_posix())
    return found


def test_serial_marker_is_only_used_by_registered_files() -> None:
    unregistered = sorted(serial_marked_files() - set(SERIAL_ALLOWED))
    assert not unregistered, (
        f"{unregistered} use pytest.mark.serial without an entry (and a reason) in "
        "tests/_serial_marker_registry.py. The serial gate step has a 300 s limit: keep tests that do "
        "not need shared state in the parallel step."
    )


def test_registry_has_no_stale_entries_and_every_entry_has_a_reason() -> None:
    stale = sorted(set(SERIAL_ALLOWED) - serial_marked_files())
    assert not stale, f"registered but no longer serial, remove from the registry: {stale}"
    assert all(reason.strip() for reason in SERIAL_ALLOWED.values())
