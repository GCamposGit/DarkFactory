"""Behavior tests for the complexity ratchet (USR-377)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "complexity_ratchet", Path(__file__).resolve().parents[1] / "scripts" / "complexity_ratchet.py"
)
assert _SPEC and _SPEC.loader
ratchet = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ratchet)


def _func(name: str, lines: int) -> str:
    return f"def {name}():\n" + "    x = 1\n" * (lines - 1)


def test_new_function_over_limit_fails_and_under_limit_passes() -> None:
    big = ratchet.measure_source(_func("big", 151))
    small = ratchet.measure_source(_func("small", 150))
    assert big == {"big": 151}
    assert ratchet.compare({"m.py::big": big["big"]}, {})
    assert ratchet.compare({"m.py::small": small["small"]}, {}) == []


def test_large_function_that_grows_fails() -> None:
    assert ratchet.compare({"m.py::f": 201}, {"m.py::f": 200})
    assert ratchet.compare({"m.py::f": 200}, {"m.py::f": 200}) == []


def test_shrinking_function_passes_and_baseline_ratchets_down() -> None:
    assert ratchet.compare({"m.py::f": 180}, {"m.py::f": 200}) == []
    assert ratchet.build_baseline({"m.py::f": 180}, {"m.py::f": 200}) == {"m.py::f": 180}
    assert ratchet.build_baseline({"m.py::f": 100}, {"m.py::f": 200}) == {}
