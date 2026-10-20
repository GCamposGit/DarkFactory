"""USR-185 slice 8: Grill thresholds (pure decision, thresholds from the registry, everything else autonomous)."""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from core.models import registry as reg
from core.models.grill_policy import GrillDecision, ModelChange, requires_grill

CAND = "deepseek/deepseek-v4-pro"
GOOD_EVIDENCE: dict[str, Any] = {
    "status": "approved",
    "budget_exceeded": False,
    "steps": [{"step": "contract", "passed": True}, {"step": "tools", "passed": True}, {"step": "reference:a", "passed": True}],
}


def _registry(mutate: Any = None) -> reg.ModelRegistry:
    raw = copy.deepcopy(reg._EMBEDDED_FALLBACK)
    if mutate:
        mutate(raw)
    return reg.parse_registry(raw)


def _model(raw: dict[str, Any], model_id: str) -> dict[str, Any]:
    return next(m for m in raw["models"] if m["id"] == model_id)


def _qualify(raw: dict[str, Any], model_id: str = CAND, **kw: Any) -> None:
    m = _model(raw, model_id)
    m["price_input_per_mtok"] = kw.pop("pin", 0.5)
    m["price_output_per_mtok"] = kw.pop("pout", 2.0)
    m["qualification"] = kw.pop("qualification", {"status": "approved", "evidence_path": "e.json"})
    m.update(kw)


def _promote(r: reg.ModelRegistry, evidence: Optional[dict[str, Any]] = GOOD_EVIDENCE) -> GrillDecision:
    return requires_grill(ModelChange("promote_candidate", CAND, evidence=evidence), r)


def _has(d: GrillDecision, text: str) -> bool:
    return d.required and any(text in r for r in d.reasons)


def test_clean_promotion_is_autonomous() -> None:
    assert _promote(_registry(_qualify)) == GrillDecision(False, [])


def test_default_roles_and_changes_are_autonomous() -> None:
    r = reg.embedded_registry()
    for role, mid in r.roles.items():
        assert not requires_grill(ModelChange("assign_role", mid, role), r).required, role
    assert not requires_grill(ModelChange("assign_role", "claude-opus-5-5", "development.claude"), r).required
    assert not requires_grill(ModelChange("swap_reviewer", "gpt-sol", "review.codex"), r).required  # same family as before


def test_forbidden_model() -> None:
    d = requires_grill(ModelChange("promote_candidate", "claude-fable-5-1"), reg.embedded_registry())
    assert _has(d, "proibido") and _has(d, "frontier")


def test_forbidden_by_marker_only() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw)
        raw["forbidden_autonomous_markers"].append("v4-pro")

    assert _has(_promote(_registry(mut)), "proibido")


def test_frontier_tier_configurable() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw)
        raw["grill_thresholds"] = {**raw["grill_thresholds"], "frontier_tiers": ["high", "frontier"]}

    assert _has(_promote(_registry(mut)), "frontier")


@pytest.mark.parametrize("field,reason", [("pin", "entrada"), ("pout", "saida")])
def test_price_above_ceiling(field: str, reason: str) -> None:
    d = _promote(_registry(lambda raw: _qualify(raw, **{field: 1000.0})))
    assert _has(d, f"preco de {reason}") and len(d.reasons) == 1


def test_price_ceiling_comes_from_registry() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw, pin=5.0)
        raw["grill_thresholds"]["max_price_input_per_mtok"] = 1.0

    assert _has(_promote(_registry(mut)), "teto 1")
    assert not _promote(_registry(lambda raw: _qualify(raw, pin=5.0))).required


def test_unknown_price_only_for_non_active() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw)
        _model(raw, CAND)["price_input_per_mtok"] = None

    r = _registry(mut)
    assert _has(_promote(r), "preco desconhecido")
    assert not requires_grill(ModelChange("assign_role", "grok-4.7", "development.grok"), r).required  # active, null price

    def off(raw: dict[str, Any]) -> None:
        mut(raw)
        raw["grill_thresholds"]["require_grill_on_unknown_price"] = False

    assert not _promote(_registry(off)).required


def test_reviewer_family_change() -> None:
    d = requires_grill(ModelChange("swap_reviewer", "grok-4.6", "review.codex"), reg.embedded_registry())
    assert _has(d, "mudanca de familia")


def test_reviewer_same_family_as_implementer() -> None:
    d = requires_grill(ModelChange("swap_reviewer", "claude-opus-5-5", "review.codex"), reg.embedded_registry())
    assert _has(d, "mesma familia") and _has(d, "mudanca de familia")


def test_implementer_ending_in_reviewer_family() -> None:
    d = requires_grill(ModelChange("assign_role", "gpt-sol", "development.claude"), reg.embedded_registry())
    assert _has(d, "mesma familia") and not _has(d, "mudanca de familia")


def test_non_review_role_family_change_is_autonomous() -> None:
    assert not requires_grill(ModelChange("assign_role", "gpt-sol", "subagent.sonnet"), reg.embedded_registry()).required


@pytest.mark.parametrize(
    "qualification,evidence,text",
    [
        (None, None, "ausente"),
        ({"status": "failed"}, None, "reprovada"),
        ({"status": "approved"}, None, "sem evidencia"),
        ({"status": "approved", "evidence_path": "e.json"}, {**GOOD_EVIDENCE, "budget_exceeded": True}, "orcamento"),
        ({"status": "approved", "evidence_path": "e.json"}, {**GOOD_EVIDENCE, "steps": GOOD_EVIDENCE["steps"][:2]}, "puladas"),
        (
            {"status": "approved", "evidence_path": "e.json"},
            {**GOOD_EVIDENCE, "steps": [*GOOD_EVIDENCE["steps"][:2], {"step": "x", "passed": False}]},
            "etapa reprovada",
        ),
    ],
)
def test_ambiguous_qualification(qualification: Optional[dict[str, Any]], evidence: Optional[dict[str, Any]], text: str) -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw)
        if qualification is None:
            _model(raw, CAND).pop("qualification")
        else:
            _model(raw, CAND)["qualification"] = qualification

    assert _has(_promote(_registry(mut), evidence), text)


def test_min_steps_threshold_from_registry() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw)
        raw["grill_thresholds"]["min_qualification_steps"] = 5

    assert _has(_promote(_registry(mut)), "puladas")


def test_combined_reasons_are_all_reported() -> None:
    def mut(raw: dict[str, Any]) -> None:
        _qualify(raw, "gpt-6-astra", pin=100.0, pout=200.0, qualification={"status": "failed"})

    d = requires_grill(ModelChange("promote_candidate", "gpt-6-astra"), _registry(mut))
    assert d.required
    for text in ("proibido", "frontier", "preco de entrada", "preco de saida", "reprovada"):
        assert _has(d, text), text
    assert len(d.reasons) == len(set(d.reasons))


def test_promote_non_candidate_and_bad_input() -> None:
    r = reg.embedded_registry()
    assert _has(requires_grill(ModelChange("promote_candidate", "gpt-sol"), r), "nao e candidate")
    assert _has(requires_grill(ModelChange("promote_candidate", "nope"), r), "desconhecido")
    assert _has(requires_grill(ModelChange("assign_role", "gpt-sol"), r), "sem papel")
    assert _has(requires_grill(ModelChange("explode", "gpt-sol"), r), "tipo de mudanca")  # type: ignore[arg-type]


def test_decision_is_pure() -> None:
    r = reg.embedded_registry()
    change = ModelChange("swap_reviewer", "grok-4.6", "review.codex")
    assert requires_grill(change, r) == requires_grill(change, r)
    assert r == reg.embedded_registry()


def test_thresholds_validated_and_fallback_mirrors_file() -> None:
    file_raw = json.loads(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    from_file = reg.parse_registry(file_raw).grill_thresholds
    assert from_file == reg.embedded_registry().grill_thresholds
    assert file_raw["grill_thresholds"] == reg._EMBEDDED_FALLBACK["grill_thresholds"]
    assert reg.grill_thresholds(reg.embedded_registry()) == from_file
    for bad in ({"max_price_input_per_mtok": -1}, {"min_qualification_steps": 0}, {"unknown": 1}):
        raw = copy.deepcopy(reg._EMBEDDED_FALLBACK)
        raw["grill_thresholds"] = {**raw["grill_thresholds"], **bad}
        with pytest.raises(ValidationError):
            reg.parse_registry(raw)


def test_registry_without_block_uses_defaults() -> None:
    raw = copy.deepcopy(reg._EMBEDDED_FALLBACK)
    raw.pop("grill_thresholds")
    assert reg.parse_registry(raw).grill_thresholds == reg.embedded_registry().grill_thresholds
