"""Regressões da separação entre IDs de CLI e gateway (USR-496)."""

from core.models.registry import cli_id, gateway_id, id_of


def test_grok_review_uses_distinct_cli_and_gateway_ids() -> None:
    assert cli_id("grok-review") == "grok-4.6"
    assert gateway_id("grok-review") == "x-ai/grok-4.6"
    assert id_of("grok-4.6") == id_of("grok-review")


def test_haiku_uses_distinct_cli_and_gateway_ids() -> None:
    assert cli_id("haiku") == "claude-haiku-5-5"
    assert gateway_id("haiku") == "anthropic/claude-haiku-5.5"
    assert id_of("claude-haiku-5-5") == id_of("haiku")


def test_opus_wire_id_is_not_the_cli_slug() -> None:
    assert gateway_id("opus") == "anthropic/claude-opus-5.5"
    assert cli_id("opus") == "claude-opus-5-5"
    assert id_of("opus") == "claude-opus-5-5"
