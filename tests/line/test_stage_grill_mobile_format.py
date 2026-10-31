"""Tests for USR-438: Layman-friendly Telegram messages, complete option text in body,
and concise 2-3 word buttons on mobile devices.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest

from core.line.stage_grill import (
    DEFAULT_PUBLIC_HUB_URL,
    _PendingQuestion,
    _build_message,
    _clean_option_text,
    _summarize_option,
    one_tap_recommended_button,
    resolve_callback_choice,
    safe_public_hub_url,
)


def test_clean_option_text() -> None:
    assert _clean_option_text("Opção 1: Usar rota padrão da fábrica") == "Usar rota padrão da fábrica"
    assert _clean_option_text("opcao 2 - Manter modelo atual") == "Manter modelo atual"
    assert _clean_option_text("1. Implementar fallback automático") == "Implementar fallback automático"
    assert _clean_option_text("- Enviar alerta imediatamente") == "Enviar alerta imediatamente"
    assert _clean_option_text("Texto sem prefixo nenhum") == "Texto sem prefixo nenhum"


def test_summarize_option_limits_to_three_words_and_char_budget() -> None:
    # 3 words or fewer
    assert _summarize_option("Usar rota padrão da fábrica com balanceamento dinâmico") == "Usar rota padrão"
    assert _summarize_option("Manter modelo atual") == "Manter modelo atual"
    assert _summarize_option("Aprovar") == "Aprovar"
    assert _summarize_option("") == "Escolher"

    # Very long words fitting within char budget
    long_words = "Desproporcionadamente incompreensível extraordinário teste"
    summary = _summarize_option(long_words, max_words=3, max_chars=24)
    assert len(summary) <= 24
    assert len(summary.split()) <= 3


def test_safe_public_hub_url_never_returns_localhost_or_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKHUB_BASE_URL", "http://127.0.0.1:8888")
    monkeypatch.delenv("DARKHUB_PUBLIC_URL", raising=False)
    assert safe_public_hub_url() == DEFAULT_PUBLIC_HUB_URL
    assert "127.0.0.1" not in safe_public_hub_url()

    monkeypatch.setenv("DARKHUB_BASE_URL", "http://localhost:3000")
    assert safe_public_hub_url() == DEFAULT_PUBLIC_HUB_URL

    monkeypatch.setenv("DARKHUB_PUBLIC_URL", "https://darkhub.meudominio.com")
    assert safe_public_hub_url() == "https://darkhub.meudominio.com"


def test_one_tap_recommended_button_mobile_format() -> None:
    q = _PendingQuestion(
        id="q1",
        text="Qual estratégia adotar?",
        kind="business",
        options=[
            "Opção 1: Usar o Grok como plan apenas se Codex e Claude estiverem sem cota",
            "Opção 2: Continuar aguardando recarga de cota do Claude",
        ],
        recommended="Opção 1: Usar o Grok como plan apenas se Codex e Claude estiverem sem cota",
        why="Economiza cotas no horário nobre",
    )
    btn = one_tap_recommended_button("USR-438", q)
    assert btn is not None
    assert btn["callback_data"] == "cb:grill:USR-438#q1:0"
    assert btn["text"].startswith("⭐ Opção 1:")
    # Words in summary after prefix <= 3
    summary_words = btn["text"].partition("⭐ Opção 1: ")[2].split()
    assert len(summary_words) <= 3


def test_build_message_displays_full_text_and_summarized_buttons() -> None:
    opt1 = "Opção 1: Usar Grok apenas se Codex e Claude estiverem esgotados e OpenRouter como último fallback"
    opt2 = "Opção 2: Falhar imediatamente se Claude e Codex estiverem sem cota"
    q = _PendingQuestion(
        id="q1",
        text="Como prosseguir com o roteamento de modelos?",
        kind="intent",
        options=[opt1, opt2],
        recommended=opt1,
        why="Garante continuidade operacional e resiliência",
    )

    text, rows = _build_message("USR-438", [q])

    # Message body checks:
    # 1. No internal technical kind bracket like "[intent]"
    assert "[intent]" not in text
    # 2. Friendly prompt header
    assert "Alinhamento necessário" in text or "Decisão necessária" in text
    assert "USR-438" in text
    # 3. Question text and why
    assert "Como prosseguir com o roteamento de modelos?" in text
    assert "Garante continuidade operacional e resiliência" in text
    # 4. Entire full text of alternatives is present in body
    assert _clean_option_text(opt1) in text
    assert _clean_option_text(opt2) in text
    assert "⭐" in text
    assert "(Recomendada)" in text

    # Buttons checks:
    # Option 1 button
    btn1 = rows[0][0]
    assert btn1["callback_data"] == "cb:grill:USR-438#q1:0"
    assert btn1["text"].startswith("1. ")
    assert len(btn1["text"].split()) <= 4  # "1." + up to 3 words
    assert len(btn1["text"]) <= 28

    # Option 2 button
    btn2 = rows[1][0]
    assert btn2["callback_data"] == "cb:grill:USR-438#q1:1"
    assert btn2["text"].startswith("2. ")
    assert len(btn2["text"].split()) <= 4

    # One-tap recommended button
    one_tap = rows[2][0]
    assert one_tap["callback_data"] == "cb:grill:USR-438#q1:0"
    assert one_tap["text"].startswith("⭐ Opção 1:")

    # DarkHub link button
    hub_btn = rows[3][0]
    assert hub_btn["url"].startswith("https://")
    assert "127.0.0.1" not in hub_btn["url"]

    # Callbacks map back to original text via resolve_callback_choice
    assert resolve_callback_choice(q, "0") == opt1
    assert resolve_callback_choice(q, "1") == opt2
