"""Fail-closed modeling helper; real executor integration belongs to SQ-13."""

from pydantic import ValidationError

from core.security.models import (
    DataClassification, EffectDecision, SecurityProfile, SensitiveEffect,
)


def evaluate_sensitive_effect(
    profile: SecurityProfile | None,
    *,
    project_id: str,
    expected_revision: int,
    classification: str | DataClassification,
    effect: str | SensitiveEffect,
) -> EffectDecision:
    """Evaluate explicit policy permission without performing any I/O.

    Consumers still enforce ACL, target authorization and enterprise controls.
    Revalidation rejects untrusted ``model_construct`` or mutated nested input.
    """
    if profile is None:
        return EffectDecision(allowed=False, reason="profile_missing")
    try:
        profile = SecurityProfile.model_validate(profile.model_dump())
    except (ValidationError, AttributeError, TypeError, ValueError):
        return EffectDecision(allowed=False, reason="profile_invalid")
    if project_id != profile.project_id:
        return EffectDecision(allowed=False, reason="project_mismatch")
    if type(expected_revision) is not int or expected_revision != profile.revision:
        return EffectDecision(allowed=False, reason="revision_mismatch")
    try:
        data_class = DataClassification(classification)
    except (TypeError, ValueError):
        return EffectDecision(allowed=False, reason="classification_unknown")
    try:
        sensitive_effect = SensitiveEffect(effect)
    except (TypeError, ValueError):
        return EffectDecision(allowed=False, reason="effect_unknown")
    order = list(DataClassification)
    if order.index(data_class) > order.index(profile.maximum_classification):
        return EffectDecision(allowed=False, reason="classification_exceeded")
    if sensitive_effect not in profile.allowed_effects:
        return EffectDecision(allowed=False, reason="effect_not_allowed")
    return EffectDecision(allowed=True, reason="profile_permission")
