"""USR-488: review cascade levels, absence of any route and contingency records."""
from __future__ import annotations

from typing import Any, Optional

import pytest

from core.line.review_cascade import ReviewRoute, select_review_route, valid_contingency


def _pick(independent: Optional[tuple[str, Optional[str]]], any_route: Optional[tuple[str, Optional[str]]]):
    def pick(stage: str, caps: list[str], **kwargs: Any) -> Optional[tuple[str, Optional[str]]]:
        return any_route if kwargs.get("implementing_harness") is None else independent

    return pick


def _select(implementer: str, family: str, independent: Any, any_route: Any) -> Optional[ReviewRoute]:
    return select_review_route(
        _pick(independent, any_route), ["harness:claude", "harness:codex"],
        implementing_harness=implementer, families={family},
    )


def test_level_one_other_family_harness() -> None:
    route = _select("claude", "anthropic", ("codex", None), ("claude", None))
    assert (route.level, route.harness, route.contingency) == (1, "codex", False)


def test_level_two_openrouter_uses_openai_for_a_claude_writer_and_sonnet_otherwise() -> None:
    from core.models.registry import resolve

    claude = _select("claude", "anthropic", ("openrouter", "sonnet"), None)
    assert claude.level == 2 and claude.model == resolve("review.openrouter")
    codex = _select("codex", "openai", ("openrouter", "sonnet"), None)
    assert codex.level == 2 and codex.model == "sonnet"


@pytest.mark.parametrize("writer,family", [("claude", "anthropic"), ("codex", "openai"), ("grok", "xai")])
def test_level_three_same_family_other_model(writer: str, family: str) -> None:
    from core.models.registry import resolve

    route = _select(writer, family, None, (writer, None))
    assert route.level == 3 and route.harness == writer and route.contingency
    assert route.model != resolve(f"development.{writer}")


def test_level_four_same_model_adversarial_persona_records_unsupported_temperature() -> None:
    route = _select("antigravity", "google", None, ("antigravity", None))
    assert route.level == 4 and route.persona == "adversarial_distinct"
    assert route.temperature["applied"] is False and route.reason
    assert valid_contingency(route.record())


def test_no_route_at_any_level_returns_none() -> None:
    assert _select("claude", "anthropic", None, None) is None


def test_contingency_record_must_carry_level_persona_and_reason() -> None:
    assert not valid_contingency(None)
    assert not valid_contingency({"level": 1, "persona": "standard", "reason": "x"})
    assert not valid_contingency({"level": 3, "persona": "standard"})
