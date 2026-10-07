"""Held-out end-to-end evaluation of the whole factory (USR-133).

Unlike the DF-18 corpus in ``evals/`` (small two-file patches), this package
models whole-product cases: a short brief, an acceptance journey that talks only
to the public interface of the product, and a recovery scenario.  Everything
here is deterministic and offline: the replay runner consumes recorded
trajectories and never calls models or the network.
"""

from __future__ import annotations

__all__ = [
    "Corpus",
    "E2ECase",
    "E2EReport",
    "LeakScanner",
    "Trajectory",
    "agent_view",
    "build_report",
    "compare_reports",
    "load_corpus",
    "verify_report",
]


def __getattr__(name: str):
    """Lazy public API so ``python -m evals.e2e.cli`` stays free of import noise."""

    if name in {"E2ECase", "Trajectory"}:
        from evals.e2e import models

        return getattr(models, name)
    if name in {"Corpus", "agent_view", "load_corpus"}:
        from evals.e2e import corpus

        return getattr(corpus, name)
    if name == "LeakScanner":
        from evals.e2e.leakage import LeakScanner

        return LeakScanner
    if name in {"E2EReport", "build_report", "compare_reports", "verify_report"}:
        from evals.e2e import replay

        return getattr(replay, name)
    raise AttributeError(name)
