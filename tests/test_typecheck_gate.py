from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import typecheck  # noqa: E402

from core.harness.runner import check_scope_label  # noqa: E402


def test_labels_distinguish_syntax_from_types() -> None:
    assert "NOT type checking" in (check_scope_label("python -m compileall -q core") or "")
    assert check_scope_label("python scripts/typecheck.py") == "static types (mypy)"
    assert check_scope_label("python -m pytest") is None


def test_typecheck_clean_modules_pass() -> None:
    assert typecheck.run() == 0


def test_typecheck_detects_controlled_type_error(tmp_path: Path) -> None:
    root = typecheck.PROJECT_ROOT
    bad = root / "core" / "_usr173_bad_contract.py"
    bad.write_text("def public_api(x: int) -> str:\n    return x\n", encoding="utf-8")
    try:
        assert typecheck.run(("core/_usr173_bad_contract.py",)) != 0
    finally:
        bad.unlink()
