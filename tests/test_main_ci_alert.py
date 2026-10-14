"""Tests for the singleton main-CI owner-action alert (USR-247).

Everything runs against a scripted ``gh`` (an injected runner, like
``tests/test_git_autonomy_ci_gate.py``'s ``GhScript``) and a store pointed at ``tmp_path``: no
network, no real GitHub, no writes outside the test's own temp directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Sequence

from core.git import ci_checks, main_ci_alert
from core.owner_actions.models import ActionPriority, ActionStatus
from core.owner_actions.store import OwnerActionStore


class FakeGh:
    """Scripted ``gh``: only the ``run list``/``run view`` calls ``check_main`` makes."""

    def __init__(self, runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]] | None = None) -> None:
        self.runs = runs
        self.jobs = jobs or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        a = list(args)
        self.calls.append(a)
        if a[:2] == ["run", "list"]:
            return subprocess.CompletedProcess(a, 0, json.dumps(self.runs), "")
        if a[:2] == ["run", "view"] and "--json" in a:
            run_id = int(a[2])
            return subprocess.CompletedProcess(a, 0, json.dumps({"jobs": self.jobs.get(run_id, [])}), "")
        return subprocess.CompletedProcess(a, 1, "", f"unsupported: {a}")


RED_RUN = {
    "databaseId": 900,
    "conclusion": "failure",
    "headSha": "5da723e04ace7e1ee7e30321ca728d6c34036757",
    "url": "https://github.com/x/y/actions/runs/900",
    "workflowName": "DarkFac CI",
}
RED_JOBS = {
    900: [
        {
            "name": "main-validation (ubuntu-latest)",
            "conclusion": "failure",
            "url": "https://github.com/x/y/actions/runs/900/job/1",
        }
    ]
}
GREEN_RUN = {"databaseId": 901, "conclusion": "success", "headSha": "a" * 40, "url": "u"}


def red_status() -> ci_checks.MainStatus:
    return ci_checks.check_main(FakeGh([RED_RUN], RED_JOBS), Path("."))


def green_status() -> ci_checks.MainStatus:
    return ci_checks.check_main(FakeGh([GREEN_RUN]), Path("."))


def store(tmp_path: Path) -> OwnerActionStore:
    return OwnerActionStore(tmp_path / "owner_actions.json")


def test_build_main_ci_alert_is_a_critical_singleton() -> None:
    action = main_ci_alert.build_main_ci_alert(red_status())
    assert action.id == main_ci_alert.MAIN_CI_ALERT_ID == "OA-MAIN-CI"
    assert action.priority is ActionPriority.CRITICAL
    assert "5da723e" in action.title and "main-validation (ubuntu-latest)" in action.title


def test_raise_creates_one_alert_and_notifies_once(tmp_path: Path) -> None:
    active = store(tmp_path)
    notified: list[str] = []

    saved = main_ci_alert.raise_main_ci_alert(
        red_status(), store=active, notifier=lambda a: notified.append(a.id)
    )

    assert saved is not None and saved.id == main_ci_alert.MAIN_CI_ALERT_ID
    assert saved.status is ActionStatus.OPEN
    assert notified == [main_ci_alert.MAIN_CI_ALERT_ID]
    assert [a.id for a in active.list_actions()] == [main_ci_alert.MAIN_CI_ALERT_ID]


def test_red_reported_again_does_not_duplicate_or_renotify(tmp_path: Path) -> None:
    active = store(tmp_path)
    notified: list[str] = []

    def notifier(action: Any) -> None:
        notified.append(action.id)

    main_ci_alert.raise_main_ci_alert(red_status(), store=active, notifier=notifier)
    main_ci_alert.raise_main_ci_alert(red_status(), store=active, notifier=notifier)
    main_ci_alert.raise_main_ci_alert(red_status(), store=active, notifier=notifier)

    assert len(active.list_actions()) == 1
    assert notified == [main_ci_alert.MAIN_CI_ALERT_ID]  # only the first raise notifies


def test_green_closes_the_open_alert(tmp_path: Path) -> None:
    active = store(tmp_path)
    main_ci_alert.raise_main_ci_alert(red_status(), store=active)

    resolved = main_ci_alert.resolve_main_ci_alert(store=active)

    assert resolved is not None and resolved.status is ActionStatus.DONE
    current = active.get(main_ci_alert.MAIN_CI_ALERT_ID)
    assert current is not None and current.status is ActionStatus.DONE


def test_resolve_without_an_open_alert_is_a_noop(tmp_path: Path) -> None:
    active = store(tmp_path)
    assert main_ci_alert.resolve_main_ci_alert(store=active) is None

    main_ci_alert.raise_main_ci_alert(red_status(), store=active)
    main_ci_alert.resolve_main_ci_alert(store=active)
    assert main_ci_alert.resolve_main_ci_alert(store=active) is None  # already done, not re-resolved


def test_sync_dispatches_red_to_raise_and_green_to_resolve(tmp_path: Path) -> None:
    active = store(tmp_path)

    raised = main_ci_alert.sync_main_ci_status(red_status(), store=active)
    assert raised is not None and raised.status is ActionStatus.OPEN

    raised_again = main_ci_alert.sync_main_ci_status(red_status(), store=active)
    assert raised_again is not None and len(active.list_actions()) == 1  # still just the one alert

    closed = main_ci_alert.sync_main_ci_status(green_status(), store=active)
    assert closed is not None and closed.status is ActionStatus.DONE


def test_sync_ignores_an_unknown_probe(tmp_path: Path) -> None:
    active = store(tmp_path)
    unknown = ci_checks.MainStatus(state="unknown", branch="main", workflow="DarkFac CI", detail="gh failed")

    assert main_ci_alert.sync_main_ci_status(unknown, store=active) is None
    assert active.list_actions() == []


def test_a_failing_notifier_never_breaks_the_raise(tmp_path: Path) -> None:
    active = store(tmp_path)

    def exploding(_action: Any) -> None:
        raise RuntimeError("telegram down")

    saved = main_ci_alert.raise_main_ci_alert(red_status(), store=active, notifier=exploding)
    assert saved is not None and saved.id == main_ci_alert.MAIN_CI_ALERT_ID


def test_raise_without_store_or_cwd_returns_none() -> None:
    assert main_ci_alert.raise_main_ci_alert(red_status()) is None


def test_resolve_without_store_or_cwd_returns_none() -> None:
    assert main_ci_alert.resolve_main_ci_alert() is None
