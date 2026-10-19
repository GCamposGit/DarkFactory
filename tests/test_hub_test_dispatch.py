"""USR-218: Hub test runs go to an online worker; no local fallback on the VPS."""

from __future__ import annotations

from typing import List

import pytest

from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
    TestSubagentEngine,
)
from core.infra.node_liveness import NodeLiveness
from hub.backend.test_dispatch import (
    NoTestWorkerAvailable,
    dispatch_test_run,
    hub_is_remote,
    pick_online_worker,
)


def _node(node_id: str, status: str) -> NodeLiveness:
    return NodeLiveness(node_id=node_id, url=f"http://{node_id}:8080", status=status)


def _report() -> DistilledTestReport:
    return DistilledTestReport(
        verdict="PASSED", success=True, exit_code=0, duration_seconds=0.1,
        concise_summary="ok", agent_feedback="ok",
    )


class FakeEngine(TestSubagentEngine):
    def __init__(self, tmp_path) -> None:
        super().__init__(log_dir=tmp_path / "logs", project_root=tmp_path)
        self.remote_calls: List[TestExecutionInstruction] = []
        self.local_calls = 0
        self.remote_error: Exception | None = None

    def execute_remote(self, instruction):  # no network
        self.remote_calls.append(instruction)
        if self.remote_error:
            raise self.remote_error
        return _report()

    def execute(self, instruction):
        self.local_calls += 1
        return _report()


def test_prefers_idle_online_worker_over_busy():
    workers = [_node("a", "online_busy"), _node("b", "offline"), _node("c", "online")]
    assert pick_online_worker(workers).node_id == "c"
    assert pick_online_worker([_node("a", "online_busy")]).node_id == "a"
    assert pick_online_worker([_node("b", "offline")]) is None


def test_remote_hub_dispatches_to_chosen_worker(tmp_path):
    engine = FakeEngine(tmp_path)
    lister = lambda: [_node("a", "online_busy"), _node("c", "online")]  # noqa: E731
    report = dispatch_test_run(engine, TestExecutionInstruction(worker_mode="auto"), remote=True, lister=lister)
    assert engine.local_calls == 0
    sent = engine.remote_calls[0]
    assert sent.remote_worker_url == "http://c:8080"
    assert sent.allow_fallback is False
    assert report.worker_id == "c" and report.execution_mode == "remote"


def test_remote_hub_without_worker_gives_clear_error(tmp_path):
    engine = FakeEngine(tmp_path)
    with pytest.raises(NoTestWorkerAvailable) as err:
        dispatch_test_run(engine, TestExecutionInstruction(), remote=True, lister=lambda: [_node("a", "offline")])
    assert str(err.value).startswith("Nenhum worker de testes online agora:")
    assert "exit code" not in str(err.value).lower()
    assert engine.local_calls == 0 and not engine.remote_calls


def test_dispatch_failure_does_not_fall_back_locally(tmp_path):
    engine = FakeEngine(tmp_path)
    engine.remote_error = OSError("timeout")
    with pytest.raises(NoTestWorkerAvailable, match="Nenhum worker de testes online agora"):
        dispatch_test_run(engine, TestExecutionInstruction(), remote=True, lister=lambda: [_node("a", "online")])
    assert engine.local_calls == 0


def test_dev_checkout_keeps_local_execution(tmp_path):
    engine = FakeEngine(tmp_path)
    dispatch_test_run(engine, TestExecutionInstruction(), remote=False)
    assert engine.local_calls == 1 and not engine.remote_calls


def test_hub_is_remote_detection(tmp_path):
    assert hub_is_remote(tmp_path, env={}) is True  # no tests/ dir
    (tmp_path / "tests").mkdir()
    assert hub_is_remote(tmp_path, env={}) is False
    assert hub_is_remote(tmp_path, env={"DARKHUB_ENV": "production"}) is True
