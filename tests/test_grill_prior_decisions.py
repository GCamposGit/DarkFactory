from __future__ import annotations

from datetime import UTC, datetime

from core.owner_actions.decisions import render_answered_decisions
from core.owner_actions.models import OwnerAction, DecisionAnswer
from core.owner_actions.store import OwnerActionStore


def _action(action_id: str, *, project_id: str | None, blocks: list[str], note: str) -> OwnerAction:
    return OwnerAction(
        id=action_id,
        kind="decision",
        title=f"Decision {action_id}",
        status="done",
        project_id=project_id,
        blocks=blocks,
        options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        answer=DecisionAnswer(option_id="yes", note=note, answered_at=datetime.now(UTC)),
    )


def test_render_prior_decisions_scopes_by_project_or_ticket_and_redacts(tmp_path) -> None:
    store = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    store.upsert(_action("OA-042", project_id="pilot-hello", blocks=["USR-279"], note="Use pilot-hello"))
    store.upsert(_action("OA-043", project_id="other-project", blocks=["USR-279"], note="other context"))

    rendered = render_answered_decisions(
        store=store,
        project_id="pilot-hello",
        ticket_id="PH-012",
    )

    assert "OA-042" in rendered
    assert "Use pilot-hello" in rendered
    assert "OA-043" not in rendered
    assert len(rendered) <= 4000


def test_answered_decision_model_accepts_optional_project_id() -> None:
    action = _action("OA-042", project_id="pilot-hello", blocks=["USR-279"], note="D1-D5 settled")
    restored = OwnerAction.model_validate(action.to_record())
    assert restored.project_id == "pilot-hello"


def test_render_prior_decisions_returns_empty_when_none_are_relevant(tmp_path) -> None:
    store = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    store.upsert(_action("OA-042", project_id="another-project", blocks=["USR-1"], note="Unrelated"))

    assert render_answered_decisions(store=store, project_id="pilot-hello", ticket_id="PH-1") == ""


def test_render_prior_decisions_filters_unanswered_and_limits_and_truncates(tmp_path) -> None:
    store = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    store.upsert(_action("OA-042", project_id="pilot-hello", blocks=[], note="x" * 500))
    store.upsert(OwnerAction(
        id="OA-043", kind="decision", title="Open decision", status="open",
        project_id="pilot-hello", options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    ))
    store.upsert(_action("OA-044", project_id="pilot-hello", blocks=[], note="Second answered"))

    rendered = render_answered_decisions(
        store=store,
        project_id="pilot-hello",
        max_items=1,
        max_chars=120,
    )

    assert "OA-042" in rendered
    assert "OA-043" not in rendered
    assert "OA-044" not in rendered
    assert len(rendered) <= 120
    assert "x" * 500 not in rendered


def test_render_prior_decisions_matches_ticket_blocks_without_project_match(tmp_path) -> None:
    store = OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None)
    store.upsert(_action("OA-042", project_id="other-project", blocks=["PH-42"], note="Ticket scoped"))

    rendered = render_answered_decisions(store=store, project_id="pilot-hello", ticket_id="PH-42")

    assert "OA-042" in rendered
    assert "Ticket scoped" in rendered
