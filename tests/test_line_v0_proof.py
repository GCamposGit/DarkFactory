from datetime import UTC, datetime, timedelta

from core.line.v0_proof import DeliveryEvidence, evaluate_v0_proof


def _delivery(index: int, *, channel: str, project_id: str = "darkfac") -> DeliveryEvidence:
    return DeliveryEvidence(
        ticket_id=f"USR-{index}", project_id=project_id, channel=channel,
        run_id=f"run-{index}", merge_sha=f"{index:040x}", delivery_evidence="merged",
        smoke_passed=True, learning_pack_linked=True, manual_page_changed=True,
        occurred_at=(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
        owner_actions_count=0, owner_decisions_count=0, intervention_types=[],
    )


def test_v0_slots_preserve_delivery_identity_and_zero_interventions() -> None:
    deliveries = [
        _delivery(1, channel="telegram"), _delivery(2, channel="hub"),
        _delivery(3, channel="chat"), _delivery(4, channel="telegram"),
        _delivery(5, channel="hub", project_id="external-product"),
    ]
    report = evaluate_v0_proof(deliveries, now=datetime(2026, 2, 1, tzinfo=UTC))

    assert report.criterion_met is True
    assert [slot.ticket_id for slot in report.slots] == [f"USR-{i}" for i in range(1, 6)]
    assert all(slot.run_id and slot.project_id and slot.channel and slot.merge_sha for slot in report.slots)
    assert all(slot.learning_pack_ref and slot.owner_interventions_count == 0 for slot in report.slots)
    assert all(slot.owner_intervention_types == [] for slot in report.slots)


def test_v0_exposes_intervention_kinds_and_does_not_approve_when_source_degraded() -> None:
    deliveries = [
        _delivery(1, channel="telegram"), _delivery(2, channel="hub"),
        _delivery(3, channel="chat"), _delivery(4, channel="telegram"),
        _delivery(5, channel="hub", project_id="external-product"),
    ]
    deliveries[-1].owner_decisions_count = 1
    deliveries[-1].intervention_types = ["decision"]
    report = evaluate_v0_proof(
        deliveries, now=datetime(2026, 2, 1, tzinfo=UTC),
        degraded_sources=["owner_actions_read_failed"],
    )

    assert report.criterion_met is False
    assert "nao foi possivel comprovar a contagem de intervencoes humanas" in report.missing
    assert report.slots[-1].owner_interventions_count == 1
    assert report.slots[-1].owner_intervention_types == ["decision"]
