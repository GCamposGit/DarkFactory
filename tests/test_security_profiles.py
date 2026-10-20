"""Synthetic policy counterexamples; never dispatch inference or network I/O."""
import pytest
from pydantic import ValidationError

from core.enterprise.models import EnterpriseProjectConfig
from core.security.models import DataClassification, SecurityProfile, SensitiveEffect
from core.security.profiles import evaluate_sensitive_effect
from core.security.threat_model import ThreatAsset, ThreatBoundary, ThreatCase, ThreatModel


def profile(**updates):
    return SecurityProfile(project_id="darkfac", profile_id="synthetic", revision=1,
                           maximum_classification="internal", **updates)


def decision(policy, **updates):
    request = dict(project_id="darkfac", expected_revision=1,
                   classification="internal", effect="write")
    request.update(updates)
    return evaluate_sensitive_effect(policy, **request)


@pytest.mark.parametrize("effect", list(SensitiveEffect))
def test_missing_profile_denies_every_sensitive_effect(effect):
    assert decision(None, effect=effect).reason == "profile_missing"
    assert not decision(None, effect=effect).allowed


@pytest.mark.parametrize("effect", list(SensitiveEffect))
def test_presence_grants_nothing_but_explicit_effect_permission(effect):
    assert not decision(profile(), effect=effect).allowed
    assert decision(profile(allowed_effects={effect}), effect=effect).allowed


@pytest.mark.parametrize("update,reason", [
    ({"project_id": "external"}, "project_mismatch"),
    ({"expected_revision": 2}, "revision_mismatch"),
    ({"expected_revision": True}, "revision_mismatch"),
    ({"classification": "unknown"}, "classification_unknown"),
    ({"classification": "confidential"}, "classification_exceeded"),
    ({"effect": "unknown"}, "effect_unknown"),
])
def test_invalid_or_stale_request_fails_closed(update, reason):
    result = decision(profile(allowed_effects={"write"}), **update)
    assert not result.allowed and result.reason == reason


@pytest.mark.parametrize("classification", list(DataClassification))
def test_sensitivity_order_is_explicit(classification):
    assert decision(profile(allowed_effects={"write"}), classification=classification).allowed == (
        classification in {DataClassification.PUBLIC, DataClassification.INTERNAL}
    )


@pytest.mark.parametrize("updates", [
    {"schema_version": 2}, {"revision": 0}, {"revision": True},
    {"project_id": "external"}, {"maximum_classification": "unknown"},
    {"allowed_effects": {"arbitrary"}}, {"unexpected": True},
])
def test_invalid_contracts_are_rejected(updates):
    fields = dict(project_id="darkfac", profile_id="synthetic", revision=1,
                  maximum_classification="internal")
    fields.update(updates)
    with pytest.raises(ValidationError):
        SecurityProfile(**fields)


def test_unvalidated_construct_cannot_bypass_helper():
    invalid = SecurityProfile.model_construct(project_id="external", profile_id="synthetic",
                                             revision=1, maximum_classification="internal")
    assert decision(invalid).reason == "profile_invalid"


def test_reuses_enterprise_config_and_checks_scope_after_mutation():
    enterprise = EnterpriseProjectConfig(project_id="darkfac")
    policy = profile(enterprise=enterprise, allowed_effects={"write"})
    assert isinstance(policy.enterprise, EnterpriseProjectConfig)
    enterprise.project_id = "external"
    assert decision(policy).reason == "profile_invalid"


def asset(**updates):
    fields = dict(asset_id="synthetic", description="synthetic fixture", classification=None,
                  consumers=("tests",), unknowns=("classification pending",))
    fields.update(updates)
    return ThreatAsset(**fields)


def test_unknown_asset_is_explicit_and_not_a_public_default():
    assert asset().classification is None
    with pytest.raises(ValidationError):
        asset(unknowns=(), evidence=("synthetic source",))


def test_threat_inventory_round_trip_and_reference_integrity():
    boundary = ThreatBoundary(boundary_id="synthetic", source="test", destination="fixture",
                              asset_ids=("synthetic",), threats=(ThreatCase(
                                  threat_id="access", abuse="unauthorized access", impact="disclosure",
                                  control_refs=("evaluate_sensitive_effect",),
                                  test_refs=("test_missing_profile_denies_every_sensitive_effect",)),),
                              unknowns=("fixture has no runtime evidence",))
    model = ThreatModel(project_id="darkfac", module="security", revision=1,
                        assets=(asset(),), boundaries=(boundary,))
    assert ThreatModel.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        ThreatModel(project_id="darkfac", module="security", revision=1,
                    assets=(asset(), asset()))
    bad = boundary.model_copy(update={"asset_ids": ("unregistered",)})
    with pytest.raises(ValidationError):
        ThreatModel(project_id="darkfac", module="security", revision=1,
                    assets=(asset(),), boundaries=(bad,))


@pytest.mark.parametrize("coverage", [{}, {"control_refs": ("ACL",)},
    {"test_refs": ("test_acl",)}, {"not_applicable_reason": " "},
    {"not_applicable_reason": "isolated", "control_refs": ("ACL",)}])
def test_uncovered_or_ambiguous_threat_is_rejected(coverage):
    with pytest.raises(ValidationError):
        ThreatCase(threat_id="synthetic", abuse="abuse", impact="impact", **coverage)


def test_justified_not_applicable_threat_and_blank_evidence():
    assert ThreatCase(threat_id="synthetic", abuse="abuse", impact="impact",
                      not_applicable_reason="No executor exists in pure helper").not_applicable_reason
    with pytest.raises(ValidationError):
        asset(evidence=(" ",))


@pytest.mark.parametrize("schema_version", [True, False, 1.0, "1"])
def test_schema_version_never_coerces_boolean_float_or_string(schema_version):
    with pytest.raises(ValidationError):
        profile(schema_version=schema_version)
    with pytest.raises(ValidationError):
        ThreatModel(project_id="darkfac", module="security", revision=1,
                    schema_version=schema_version, assets=(asset(),))


def test_exact_integer_schema_version_is_accepted():
    assert profile(schema_version=1).schema_version == 1
    assert ThreatModel(project_id="darkfac", module="security", revision=1,
                       schema_version=1, assets=(asset(),)).schema_version == 1
