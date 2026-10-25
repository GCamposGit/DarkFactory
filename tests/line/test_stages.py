"""Contrato de artefatos por estagio (USR-375 S2): ausente / malformado / valido."""

from __future__ import annotations

import json

import pytest

from core.line import workspace
from core.line.stages import STAGE_SEQUENCE, Stage, StageArtifactMissing, require_artifact

_TICKET = [{"id": "T1", "title": "t"}]
_VALID = {
    Stage.INTAKE: {"DEMAND.md": "demanda"},
    Stage.GRILL: {"GRILL.md": "grill"},
    Stage.PLAN: {"SPEC.md": "# spec", "tickets.json": json.dumps(_TICKET)},
    Stage.BUILD: {"progress.json": json.dumps({"tickets_done": ["T1"]})},
    Stage.VALIDATE: {"validation.json": json.dumps({"passed": True})},
    Stage.REVIEW: {"review_verdict.json": json.dumps({"verdict": "approve"})},
    Stage.MERGE: {"merge.json": json.dumps({"pr_url": "u", "merge_sha": "abc"})},
    Stage.DEPLOY_SMOKE: {"deploy_report.json": json.dumps({"sha": "abc", "smoke_ok": True})},
}


def _ws(tmp_path):
    return workspace.RunWorkspace(
        project_id="p", run_id="r1", branch="b", path=tmp_path, base_sha="0"
    )


def test_sequence_is_canonical():
    assert [s.value for s in STAGE_SEQUENCE] == [
        "INTAKE", "GRILL", "PLAN", "RESEARCH", "BUILD", "VALIDATE",
        "REVIEW", "MERGE", "DEPLOY_SMOKE", "DOCS", "LEARNING",
    ]  # fmt: skip


@pytest.mark.parametrize("stage", list(_VALID))
def test_require_artifact_absent_malformed_valid(stage, tmp_path):
    ws = _ws(tmp_path)
    with pytest.raises(StageArtifactMissing) as exc:
        require_artifact(stage, ws)
    assert exc.value.stage is stage and exc.value.reason

    for name in _VALID[stage]:
        workspace.write_context(ws, name, "{not json" if name.endswith(".json") else " ")
    with pytest.raises(StageArtifactMissing):
        require_artifact(stage, ws)

    for name, content in _VALID[stage].items():
        workspace.write_context(ws, name, content)
    assert require_artifact(stage, ws) is not None


@pytest.mark.parametrize("stage", [Stage.RESEARCH, Stage.DOCS, Stage.LEARNING])
def test_optional_stages_return_none(stage, tmp_path):
    assert require_artifact(stage, _ws(tmp_path)) is None
