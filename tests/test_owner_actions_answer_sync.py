"""USR-195: an answer given in the Hub reaches the repository ledger without a human or an agent."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.owner_actions.answer_sync import (
    DEMANDS_RELATIVE,
    OWNER_ACTIONS_RELATIVE,
    sync_answers_to_repo,
)
from core.owner_actions.models import ActionStatus, OwnerActionDraft
from core.owner_actions.store import OwnerActionStore


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Temporary repo with a local bare origin, one open decision (OA-001) blocking USR-1."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "-b", "main", cwd=origin)
    root = tmp_path / "work"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.name", "Test User", cwd=root)
    _git("config", "user.email", "test@darkfac.local", cwd=root)
    OwnerActionStore(root / OWNER_ACTIONS_RELATIVE, remote_ref=None).add(
        OwnerActionDraft.model_validate(
            {
                "kind": "decision",
                "title": "Escolher a retencao",
                "why": "Bloqueia o ticket",
                "blocks": ["USR-1"],
                "options": [{"id": "A", "label": "Opcao A"}, {"id": "B", "label": "Opcao B"}],
            }
        )
    )
    DemandsStore(root / DEMANDS_RELATIVE).save_ticket(
        UserTicket(id="USR-1", title="Ticket bloqueado", problem_statement="Contexto original")
    )
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "seed", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "origin", "main", cwd=root)
    return root


def _hub_overlay(tmp_path: Path, repo: Path) -> OwnerActionStore:
    """The Hub: same definitions (shipped in the image) plus the durable resolutions overlay."""
    return OwnerActionStore(
        repo / OWNER_ACTIONS_RELATIVE, overlay_path=tmp_path / "hub" / "resolutions.json", remote_ref=None
    )


def _fetcher(hub: OwnerActionStore):
    return lambda: [a.to_record() for a in hub.answered_decisions()]


def _origin_head(repo: Path) -> str:
    return _git("ls-remote", "origin", "refs/heads/main", cwd=repo)


def test_hub_answer_is_persisted_without_any_push_and_is_idempotent(tmp_path: Path, repo: Path) -> None:
    hub = _hub_overlay(tmp_path, repo)
    before = _origin_head(repo)

    # Nothing answered yet: nothing pending.
    assert sync_answers_to_repo(repo, _fetcher(hub)).pending == []

    hub.resolve("OA-001", option_id="B", note="vale 30 dias", answered_by="owner", to_overlay=True)

    result = sync_answers_to_repo(repo, _fetcher(hub))
    assert result.error == ""
    assert result.delivered is True
    assert result.applied == ["OA-001"]
    assert result.annotated == {"OA-001": ["USR-1"]}

    # USR-343 S4b: no worktree, no branch, no push -- origin is exactly where it was.
    assert _origin_head(repo) == before
    assert _git("branch", "--list", "ticket/*", cwd=repo).strip() == ""
    action = OwnerActionStore(repo / OWNER_ACTIONS_RELATIVE, remote_ref=None).get("OA-001")
    assert action is not None and action.status is ActionStatus.DONE
    assert action.answer is not None and action.answer.option_id == "B"
    ticket = DemandsStore(repo / DEMANDS_RELATIVE).get_ticket("USR-1")
    assert ticket is not None
    assert "owner-decision:OA-001=B" in ticket.tags
    assert "[Decisao do owner OA-001]" in ticket.problem_statement
    assert "vale 30 dias" in ticket.problem_statement

    again = sync_answers_to_repo(repo, _fetcher(hub))
    assert again.pending == [] and again.delivered is False


def test_control_store_receives_the_answer_and_the_file_stays_untouched(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    hub = _hub_overlay(tmp_path, repo)
    hub.resolve("OA-001", option_id="A", to_overlay=True)
    file_before = (repo / OWNER_ACTIONS_RELATIVE).read_text(encoding="utf-8")

    result = sync_answers_to_repo(repo, _fetcher(hub), control=True)

    assert result.applied == ["OA-001"]
    assert (repo / OWNER_ACTIONS_RELATIVE).read_text(encoding="utf-8") == file_before
    from core.owner_actions.control_queue import list_control_rows

    row = next(r for r in list_control_rows() if r["id"] == "OA-001")
    assert row["status"] == "done" and row["answer"]["option_id"] == "A"


def test_annotation_alone_is_repaired_when_decision_is_done_but_ticket_lacks_tag(tmp_path: Path, repo: Path) -> None:
    """Decision recorded by an agent earlier but the blocked ticket was never annotated."""
    hub = _hub_overlay(tmp_path, repo)
    hub.resolve("OA-001", option_id="A", to_overlay=True)
    OwnerActionStore(repo / OWNER_ACTIONS_RELATIVE, remote_ref=None).resolve("OA-001", option_id="A")

    result = sync_answers_to_repo(repo, _fetcher(hub))

    assert result.delivered is True and result.applied == []
    assert result.annotated == {"OA-001": ["USR-1"]}


def test_failures_never_raise_and_hide_nothing_sensitive(tmp_path: Path, repo: Path) -> None:
    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("Hub answered HTTP 503")

    result = sync_answers_to_repo(repo, boom)
    assert result.delivered is False and "503" in result.error


def test_answers_endpoint_requires_telemetry_key_and_lists_overlay_answers(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hub.backend.api import get_hub_service, router

    hub = _hub_overlay(tmp_path, repo)
    hub.resolve("OA-001", option_id="B", to_overlay=True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hub_service] = lambda: SimpleNamespace(owner_action_store=hub)
    with TestClient(app) as client:
        monkeypatch.delenv("DARKFAC_TELEMETRY_KEY", raising=False)
        assert client.get("/api/owner-actions/answers").status_code == 503
        monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-machine-key")
        assert client.get("/api/owner-actions/answers").status_code == 401
        assert client.get("/api/owner-actions/answers", headers={"X-DarkFac-Telemetry-Key": "wrong"}).status_code == 401
        ok = client.get("/api/owner-actions/answers", headers={"X-DarkFac-Telemetry-Key": "test-machine-key"})
    assert ok.status_code == 200
    rows = ok.json()["answers"]
    assert [(r["id"], r["answer"]["option_id"]) for r in rows] == [("OA-001", "B")]
