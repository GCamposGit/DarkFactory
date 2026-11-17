"""`ticket_engine` BUILD-slice resume primitives (USR-475 T1).

A BUILD interrupted mid-plan used to be invisible between slices: `progress.json` was only
written once, after every slice finished. A resumed attempt therefore reset PLAN and replayed
every already-finished slice. These primitives let the launcher persist each slice as it
finishes and tell a genuinely resumable artifact apart from one that no longer matches the
current plan/candidate/worktree (which must fail closed, never silently reset or replay).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.line import ticket_engine, workspace


def _rws(tmp_path: Path, run_id: str = "USR-900") -> workspace.RunWorkspace:
    return workspace.RunWorkspace(
        project_id="darkfac", run_id=run_id, branch=f"ticket/{run_id.lower()}",
        path=tmp_path / "artifacts", base_sha="0" * 40,
    )


def _write_plan(ws: workspace.RunWorkspace, tickets: list[dict]) -> None:
    workspace.write_context(ws, ticket_engine.SPEC_FILE, "# SPEC\n\n## Objetivo\nTeste\n")
    workspace.write_context(ws, ticket_engine.TICKETS_FILE, json.dumps(tickets))


def _write_legacy_build(ws: workspace.RunWorkspace, harness: str, tickets_done: list[str]) -> None:
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"harness": harness, "tickets_done": tickets_done}), encoding="utf-8")


def test_resumable_build_is_none_without_a_build_artifact(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    assert ticket_engine.resumable_build(ws, "candidate-x", "worktree-x") is None


def test_legacy_complete_build_migrates_atomically_and_idempotently(tmp_path: Path) -> None:
    """A complete legacy report is bound to current evidence once, then stays unchanged."""
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    _write_legacy_build(ws, "grok", ["T1"])  # pre-USR-475 shape: no digests at all

    report = ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")

    assert report is not None
    assert report.ticket_id == ws.run_id
    assert report.tickets_done == ["T1"]
    assert report.completed_slice_count == 1
    assert report.plan_digest == ticket_engine.plan_digest(ws)
    assert report.candidate_digest == "candidate-x"
    assert report.worktree == "worktree-x"
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    migrated = path.read_bytes()
    assert ticket_engine.resumable_build(ws, "candidate-x", "worktree-x") == report
    assert path.read_bytes() == migrated


def test_partial_legacy_build_fails_closed_without_mutating_progress(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    _write_legacy_build(ws, "grok", ["T1"])
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    before = path.read_bytes()

    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="checkpoint legado"):
        ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")

    assert path.read_bytes() == before


def test_invalid_checkpoint_fails_closed_without_replanning_or_rewriting(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"tickets_done": [', encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="invalido ou ilegivel"):
        ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")

    assert path.read_bytes() == before


def test_resumable_build_returns_the_report_when_plan_candidate_and_worktree_all_match(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    digest = ticket_engine.plan_digest(ws)
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=digest, candidate_digest="candidate-x", worktree="worktree-x",
    )

    report = ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")

    assert report is not None
    assert report.ticket_id == ws.run_id
    assert report.tickets_done == ["T1"]
    assert report.completed_slice_count == 1
    assert report.harness == "grok"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda ws, digest: None, id="candidate_diverges"),
        pytest.param(
            lambda ws, digest: workspace.write_context(
                ws, ticket_engine.TICKETS_FILE, json.dumps([{"id": "T1", "title": "a changed"}])
            ),
            id="plan_diverges",
        ),
    ],
)
def test_resumable_build_raises_build_evidence_mismatch_on_divergence(
    tmp_path: Path, mutate,
) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    digest = ticket_engine.plan_digest(ws)
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=digest, candidate_digest="candidate-old", worktree="worktree-x",
    )
    mutate(ws, digest)

    with pytest.raises(ticket_engine.BuildEvidenceMismatch):
        ticket_engine.resumable_build(ws, "candidate-new", "worktree-x")


def test_resumable_build_raises_build_evidence_mismatch_on_worktree_divergence(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    digest = ticket_engine.plan_digest(ws)
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=digest, candidate_digest="candidate-x", worktree="worktree-old",
    )

    with pytest.raises(ticket_engine.BuildEvidenceMismatch):
        ticket_engine.resumable_build(ws, "candidate-x", "worktree-new")


def test_resumable_build_rejects_completed_ids_that_are_not_a_plan_prefix(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    ticket_engine.write_build(
        ws, "grok", ["T2"], plan_digest=ticket_engine.plan_digest(ws),
        candidate_digest="candidate-x", worktree="worktree-x",
    )

    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="prefixo ordenado"):
        ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")


def test_resumable_build_mismatch_does_not_touch_the_preserved_artifact(tmp_path: Path) -> None:
    """Fail closed means fail closed: raising must not delete or rewrite progress.json/PLAN."""
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    digest = ticket_engine.plan_digest(ws)
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=digest, candidate_digest="candidate-old", worktree="worktree-x",
    )
    from core.line.stages import BUILD_FILE

    progress_path = workspace.context_dir(ws) / BUILD_FILE
    before = progress_path.read_text(encoding="utf-8")

    with pytest.raises(ticket_engine.BuildEvidenceMismatch):
        ticket_engine.resumable_build(ws, "candidate-new", "worktree-x")

    assert progress_path.read_text(encoding="utf-8") == before


def test_plan_digest_changes_when_the_plan_content_changes(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    first = ticket_engine.plan_digest(ws)
    _write_plan(ws, [{"id": "T1", "title": "a changed"}])
    second = ticket_engine.plan_digest(ws)
    assert first != second


def test_plan_digest_is_stable_for_identical_content(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])
    assert ticket_engine.plan_digest(ws) == ticket_engine.plan_digest(ws)


def test_write_build_round_trips_digest_fields(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    ticket_engine.write_build(
        ws, "codex", ["T1", "T2"],
        plan_digest="plan-abc", candidate_digest="cand-abc", worktree="/wt/abc",
    )
    from core.line.stages import BUILD_FILE

    saved = json.loads((workspace.context_dir(ws) / BUILD_FILE).read_text(encoding="utf-8"))
    assert saved == {
        "harness": "codex", "ticket_id": ws.run_id, "tickets_done": ["T1", "T2"],
        "completed_slice_count": 2,
        "plan_digest": "plan-abc", "candidate_digest": "cand-abc", "worktree": "/wt/abc",
    }


def test_write_build_rejects_new_checkpoint_without_complete_identity(tmp_path: Path) -> None:
    ws = _rws(tmp_path)

    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="identidade completa"):
        ticket_engine.write_build(ws, "codex", ["T1"])

    from core.line.stages import BUILD_FILE

    assert not (workspace.context_dir(ws) / BUILD_FILE).exists()


def test_write_build_allows_a_durable_empty_prefix_before_first_slice(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}])

    ticket_engine.write_build(
        ws, "codex", [], plan_digest=ticket_engine.plan_digest(ws), candidate_digest="cand-abc",
        worktree="/wt/abc",
    )

    report = ticket_engine.resumable_build(ws, "cand-abc", "/wt/abc")
    assert report is not None and report.tickets_done == []
    assert report.ticket_id == ws.run_id and report.completed_slice_count == 0


def test_write_build_replace_failure_leaves_previous_checkpoint_intact(tmp_path: Path, monkeypatch) -> None:
    ws = _rws(tmp_path)
    ticket_engine.write_build(
        ws, "codex", ["T1"], plan_digest="plan-old", candidate_digest="cand-old", worktree="/wt/old",
    )
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    before = path.read_bytes()
    monkeypatch.setattr(ticket_engine.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        ticket_engine.write_build(
            ws, "codex", ["T1", "T2"],
            plan_digest="plan-old", candidate_digest="cand-next", worktree="/wt/old",
        )

    assert path.read_bytes() == before


def test_write_build_rejects_checkpoint_regression_and_candidate_drift_without_progress(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    digest = ticket_engine.plan_digest(ws)
    identity = {"plan_digest": digest, "candidate_digest": "candidate-1", "worktree": "/wt/one"}
    ticket_engine.write_build(ws, "codex", ["T1", "T2"], **identity)
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    before = path.read_bytes()
    for tickets, candidate in ((["T1"], "candidate-1"), (["T1", "T2"], "candidate-2")):
        with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="regredir|Candidato|candidato"):
            ticket_engine.write_build(
                ws, "codex", tickets, plan_digest=digest,
                candidate_digest=candidate, worktree="/wt/one",
            )
        assert path.read_bytes() == before


def test_write_build_allows_candidate_to_advance_only_with_a_new_completed_prefix(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    digest = ticket_engine.plan_digest(ws)
    ticket_engine.write_build(
        ws, "codex", [], plan_digest=digest, candidate_digest="candidate-0", worktree="/wt/one",
    )
    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="candidato mudou"):
        ticket_engine.write_build(
            ws, "codex", [], plan_digest=digest, candidate_digest="candidate-1", worktree="/wt/one",
        )

    ticket_engine.write_build(
        ws, "codex", ["T1"], plan_digest=digest, candidate_digest="candidate-1", worktree="/wt/one",
    )
    ticket_engine.write_build(
        ws, "codex", ["T1", "T2"], plan_digest=digest,
        candidate_digest="candidate-2", worktree="/wt/one",
    )
    report = ticket_engine.resumable_build(ws, "candidate-2", "/wt/one")
    assert report is not None and report.tickets_done == ["T1", "T2"]


def test_resumable_build_migrates_the_initial_classified_checkpoint_with_ticket_and_index(
    tmp_path: Path,
) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "harness": "grok", "tickets_done": ["T1"],
        "plan_digest": ticket_engine.plan_digest(ws),
        "candidate_digest": "candidate-x", "worktree": "worktree-x",
    }), encoding="utf-8")

    report = ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")

    assert report is not None
    assert report.ticket_id == ws.run_id and report.completed_slice_count == 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ticket_id"] == ws.run_id and saved["completed_slice_count"] == 1


def test_resumable_build_fails_closed_when_ticket_or_completed_index_diverges(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}])
    from core.line.stages import BUILD_FILE

    path = workspace.context_dir(ws) / BUILD_FILE
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=ticket_engine.plan_digest(ws),
        candidate_digest="candidate-x", worktree="worktree-x",
    )
    original = json.loads(path.read_text(encoding="utf-8"))
    for field, value, cause in (
        ("ticket_id", "USR-elsewhere", "pertence ao ticket"),
        ("completed_slice_count", 2, "completed_slice_count diverge"),
    ):
        divergent = {**original, field: value}
        path.write_text(json.dumps(divergent), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(ticket_engine.BuildEvidenceMismatch, match=cause):
            ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")
        assert path.read_bytes() == before


def test_resumable_build_rejects_duplicate_plan_slice_ids(tmp_path: Path) -> None:
    ws = _rws(tmp_path)
    _write_plan(ws, [{"id": "T1", "title": "a"}, {"id": "T1", "title": "repeated"}])
    ticket_engine.write_build(
        ws, "grok", ["T1"], plan_digest=ticket_engine.plan_digest(ws),
        candidate_digest="candidate-x", worktree="worktree-x",
    )

    with pytest.raises(ticket_engine.BuildEvidenceMismatch, match="ids de fatia duplicados"):
        ticket_engine.resumable_build(ws, "candidate-x", "worktree-x")
