"""Protected DF-15 verifier, intentionally independent of the visible fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def verify(workdir: Path) -> dict:
    candidate = (workdir / "calculator.py").resolve()
    if not candidate.is_file():
        return {"verdict": "FAIL", "discovered_count": 3, "passed_count": 0, "details": {"error": "candidate missing"}}
    spec = importlib.util.spec_from_file_location("df15_candidate_calculator", candidate)
    if spec is None or spec.loader is None:
        return {"verdict": "FAIL", "discovered_count": 3, "passed_count": 0, "details": {"error": "candidate cannot load"}}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    add = getattr(module, "add", None)
    cases = ((0, 0, 0), (-7, 4, -3), (13, -5, 8))
    passed = 0
    failures: list[str] = []
    if not callable(add):
        failures.append("add is not callable")
    else:
        for left, right, expected in cases:
            try:
                actual = add(left, right)
            except Exception as exc:  # pragma: no cover - evidence in result
                failures.append(f"{left}+{right} raised {type(exc).__name__}")
                continue
            if actual == expected:
                passed += 1
            else:
                failures.append(f"{left}+{right} returned {actual!r}")
    return {
        "verdict": "PASS" if passed == len(cases) else "FAIL",
        "discovered_count": len(cases),
        "passed_count": passed,
        "details": {"failures": failures, "cases": len(cases)},
    }

