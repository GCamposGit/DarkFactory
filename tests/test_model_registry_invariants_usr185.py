"""USR-185 slice 3: protected-rule invariants verified against the model registry (passes without the protected patch)."""

from __future__ import annotations

import pytest

from core.models import registry as reg

REGISTRY = reg.get_registry()
BY_ID = {m.id: m for m in REGISTRY.models}


def test_quota_floor_is_at_least_15_percent() -> None:
    assert REGISTRY.quota_floor_percent >= 15.0
    assert reg.QUOTA_FLOOR_MIN == 15.0


def test_quota_floor_below_15_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["quota_floor_percent"] = 14.9
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


@pytest.mark.parametrize("name", ["fable", "astra"])
def test_fable_and_astra_are_banned_from_autonomous_use(name: str) -> None:
    assert name in reg.forbidden_markers()
    with pytest.raises(KeyError):
        reg.resolve(name)
    with pytest.raises(KeyError):
        reg.resolve_known(name)
    entry = next(m for m in REGISTRY.models if name in m.id or name in m.aliases)
    assert entry.forbidden_autonomous is True
    assert entry.id not in REGISTRY.roles.values()


def test_no_role_routes_to_a_forbidden_model() -> None:
    markers = [m.lower() for m in reg.forbidden_markers()]
    for role, model_id in REGISTRY.roles.items():
        assert not BY_ID[model_id].forbidden_autonomous, role
        assert not any(mk in model_id.lower() for mk in markers), role


def test_forbidden_role_binding_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["roles"]["development.claude"] = "claude-fable-5-1"
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_reviewer_is_always_a_different_family_than_implementer() -> None:
    assert REGISTRY.review_pairs
    for pair in REGISTRY.review_pairs:
        impl = BY_ID[REGISTRY.roles[pair.implementer]]
        rev = BY_ID[REGISTRY.roles[pair.reviewer]]
        assert impl.family != rev.family, pair
    # Haiku never reviews.
    assert "claude-haiku-5-5" not in {REGISTRY.roles[p.reviewer] for p in REGISTRY.review_pairs}


def test_same_family_reviewer_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["roles"]["review.codex"] = REGISTRY.roles["development.claude"]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_roles_only_route_to_active_models() -> None:
    for role, model_id in REGISTRY.roles.items():
        assert BY_ID[model_id].status == "active", role


def test_gateway_id_without_slash_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["models"][0]["gateway_ids"] = ["no-slash-slug"]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_cli_id_with_slash_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["models"][0]["cli_ids"] = ["provider/not-a-cli-slug"]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_cli_id_colliding_with_another_models_alias_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    other_alias = raw["models"][1]["aliases"][0]
    raw["models"][0]["cli_ids"] = [other_alias]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_cli_id_colliding_with_another_models_id_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["models"][0]["cli_ids"] = [raw["models"][1]["id"]]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)


def test_cli_id_equal_to_the_models_own_id_is_accepted() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["models"][0]["cli_ids"] = [raw["models"][0]["id"]]
    parsed = reg.parse_registry(raw)
    assert parsed.models[0].cli_ids == [raw["models"][0]["id"]]


def test_alias_equal_to_the_models_own_id_is_rejected() -> None:
    raw = REGISTRY.model_dump(mode="json")
    raw["models"][0]["aliases"] = [raw["models"][0]["id"]]
    with pytest.raises(ValueError):
        reg.parse_registry(raw)
