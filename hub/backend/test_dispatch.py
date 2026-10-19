"""Dispatch of Hub-triggered test runs to an online test worker (USR-218).

The Hub container on the VPS has no ``tests/`` tree, so running pytest there ends in
exit code 4. When the Hub detects it is not a development checkout it picks an online
worker with :mod:`core.infra.node_liveness` and forwards the run to the worker's
``/execute`` endpoint (the same dispatch used by ``TestSubagentEngine``); local fallback
is blocked with an explicit message.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
    TestSubagentEngine,
)
from core.infra.node_liveness import (
    STATUS_ONLINE,
    NodeLiveness,
    list_worker_liveness,
)

logger = logging.getLogger(__name__)

NO_WORKER_PREFIX = "Nenhum worker de testes online agora"

Lister = Callable[[], List[NodeLiveness]]


class NoTestWorkerAvailable(RuntimeError):
    """No test worker could take the run; the message is safe to show in the UI."""


def hub_is_remote(project_root: Path, env: Optional[dict] = None) -> bool:
    """True when the Hub runs in production/container, i.e. without the test suite."""
    environ = os.environ if env is None else env
    if (environ.get("DARKHUB_ENV") or "").strip().lower() == "production":
        return True
    return not (Path(project_root) / "tests").is_dir()


def pick_online_worker(workers: List[NodeLiveness]) -> Optional[NodeLiveness]:
    """Prefer an idle ``online`` worker; fall back to ``online_busy`` only if none."""
    live = [w for w in workers if w.is_live and w.url]
    for worker in live:
        if worker.status == STATUS_ONLINE:
            return worker
    return live[0] if live else None


def dispatch_test_run(
    engine: TestSubagentEngine,
    instruction: TestExecutionInstruction,
    *,
    remote: Optional[bool] = None,
    lister: Optional[Lister] = None,
) -> DistilledTestReport:
    """Run ``instruction``; on a remote Hub, only through an online worker."""
    is_remote = hub_is_remote(engine.project_root) if remote is None else remote
    if not is_remote:
        return engine.execute(instruction)

    workers = (lister or list_worker_liveness)()
    chosen = pick_online_worker(workers)
    if chosen is None:
        seen = ", ".join(f"{w.node_id}={w.status}" for w in workers) or "nenhum nó no inventário"
        raise NoTestWorkerAvailable(
            f"{NO_WORKER_PREFIX}: o Hub na VPS não executa a suíte localmente e todos os nós "
            f"de teste estão fora do ar ({seen}). Ligue o Desktop, o Notebook ou o grokbox e tente de novo."
        )
    routed = instruction.model_copy(
        update={"remote_worker_url": chosen.url, "worker_mode": "remote", "allow_fallback": False}
    )
    logger.info("Dispatching Hub test run to %s (%s)", chosen.node_id, chosen.url)
    try:
        report = engine.execute_remote(routed)
    except Exception as exc:
        raise NoTestWorkerAvailable(
            f"{NO_WORKER_PREFIX}: o worker {chosen.node_id} ({chosen.url}) não respondeu à execução ({exc})."
        ) from exc
    report.worker_id = report.worker_id if report.worker_id != "local" else chosen.node_id
    report.execution_mode = "remote"
    return report
