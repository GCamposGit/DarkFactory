"""USR-189: /tickets lists open demands and splits replies under the Telegram cap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.integrations.telegram import (
    TelegramActionType,
    TelegramConfig,
    TelegramDispatchResult,
    TelegramGateway,
    reply_messages,
)
from core.integrations.telegram_tickets import (
    TELEGRAM_MESSAGE_LIMIT,
    format_open_tickets_messages,
    is_open_status,
    load_open_tickets,
    telegram_units,
)


def _row(
    ticket_id: str,
    status: str,
    title: str,
    problem: str = "",
) -> dict[str, str]:
    return {
        "id": ticket_id,
        "project_id": "darkfac",
        "title": title,
        "status": status,
        "problem_statement": problem,
    }


def _gateway(tmp_path: Path, *, role: str = "ops") -> TelegramGateway:
    config = TelegramConfig(
        bot_token="tok",
        role=role,
        authorized_user_ids=[10],
        authorized_chat_ids=[10],
    )
    return TelegramGateway(config=config, state_dir=tmp_path / "tg")


def _message(update_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": text,
        },
    }


def test_open_status_keeps_active_and_drops_closed() -> None:
    assert is_open_status("planned")
    assert is_open_status("implementing")
    assert is_open_status("validating")
    assert is_open_status("discovered")
    assert is_open_status("accepted")
    assert not is_open_status("completed")
    assert not is_open_status("cancelled")
    assert not is_open_status("canceled")


def test_format_filters_closed_and_shows_id_and_description() -> None:
    rows = [
        _row("USR-10", "planned", "Dez primeiro", "descricao dez"),
        _row("USR-2", "implementing", "Dois ativo", "descricao dois"),
        _row("USR-3", "validating", "Tres validando", "ainda aberto"),
        _row("USR-4", "discovered", "Quatro descoberto", ""),
        _row("USR-5", "accepted", "Cinco aceito", "na fila"),
        _row("USR-8", "completed", "Oito feito", "MARCA_CONCLUIDA_189"),
        _row("USR-9", "cancelled", "Nove cancelado", "MARCA_CANCELADA_189"),
    ]

    parts = format_open_tickets_messages(rows)
    text = "\n".join(parts)

    assert len(parts) == 1
    assert telegram_units(parts[0]) < TELEGRAM_MESSAGE_LIMIT
    assert "Tickets abertos</b> (5)" in text
    assert text.index("USR-2") < text.index("USR-3") < text.index("USR-4") < text.index("USR-5") < text.index("USR-10")
    assert "Dois ativo" in text and "descricao dois" in text
    assert "Quatro descoberto" in text
    assert "USR-8" not in text and "MARCA_CONCLUIDA_189" not in text
    assert "USR-9" not in text and "MARCA_CANCELADA_189" not in text
    assert parts[0].count("<code>") == parts[0].count("</code>")
    assert parts[0].count("<b>") == parts[0].count("</b>")


def test_format_escapes_html_and_collapses_description_whitespace() -> None:
    rows = [_row("USR-7", "planned", "Titulo <b>injected</b>", "linha\ncom & sinal")]
    text = format_open_tickets_messages(rows)[0]
    assert "<b>injected</b>" not in text
    assert "&lt;b&gt;injected&lt;/b&gt;" in text
    assert "linha com &amp; sinal" in text
    assert "USR-7" in text


def test_format_splits_long_lists_under_the_telegram_limit() -> None:
    rows = [
        _row(f"USR-{index}", "planned", f"Ticket {index}", "d" * 80)
        for index in range(1, 8)
    ]
    rows.append(_row("USR-99", "completed", "Fechado", "NAO_LISTAR"))

    parts = format_open_tickets_messages(rows, limit=220)
    assert len(parts) >= 2
    assert all(telegram_units(part) < 220 for part in parts)
    assert all(telegram_units(part) < TELEGRAM_MESSAGE_LIMIT for part in parts)
    assert all(part.count("<code>") == part.count("</code>") for part in parts)
    assert all(part.count("<b>") == part.count("</b>") for part in parts)
    joined = "\n".join(parts)
    for index in range(1, 8):
        assert f"USR-{index}" in joined
    assert "USR-99" not in joined and "NAO_LISTAR" not in joined
    assert "continua" in joined


def test_format_truncates_one_huge_description_inside_a_single_part() -> None:
    rows = [_row("USR-1", "planned", "Curto", "x" * 8000)]
    parts = format_open_tickets_messages(rows)
    assert len(parts) == 1
    assert telegram_units(parts[0]) < TELEGRAM_MESSAGE_LIMIT
    assert "USR-1" in parts[0]
    assert "..." in parts[0]


def test_format_empty_and_all_closed() -> None:
    assert "Nenhum ticket aberto" in format_open_tickets_messages([])[0]
    closed = format_open_tickets_messages([_row("USR-1", "completed", "Feito", "sumido")])
    assert "Nenhum ticket aberto" in closed[0]
    assert "USR-1" not in closed[0]


def test_load_open_tickets_reads_queue_file(tmp_path: Path) -> None:
    path = tmp_path / "demands.json"
    path.write_text(
        json.dumps(
            [
                _row("USR-2", "planned", "Aberto dois", "detalhe dois"),
                _row("USR-1", "cancelled", "Cancelado", "MARCA_CANCELADA_189"),
                _row("USR-3", "completed", "Concluido", "MARCA_CONCLUIDA_189"),
                {"id": "quebrado", "status": "planned"},
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_open_tickets(path)
    assert [ticket.id for ticket in loaded] == ["USR-2"]
    assert loaded[0].problem_statement == "detalhe dois"


def test_tickets_command_on_ops_bot_lists_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "demands.json"
    path.write_text(
        json.dumps(
            [
                _row("USR-10", "accepted", "Dez", "descricao dez"),
                _row("USR-2", "implementing", "Dois", "descricao dois"),
                _row("USR-31", "completed", "Feito", "MARCA_CONCLUIDA_189"),
                _row("USR-44", "cancelled", "Cancelado", "MARCA_CANCELADA_189"),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(path))

    gateway = _gateway(tmp_path)
    result = gateway.process_update(_message(1, "/tickets@darkfac_ops_bot"))

    assert result.action == TelegramActionType.TICKETS
    assert result.error is None
    body = "\n".join(result.response_parts)
    assert "USR-2" in body and "descricao dois" in body
    assert "USR-10" in body and "descricao dez" in body
    assert body.index("USR-2") < body.index("USR-10")
    assert "USR-31" not in body and "MARCA_CONCLUIDA_189" not in body
    assert "USR-44" not in body and "MARCA_CANCELADA_189" not in body
    assert all(telegram_units(part) < TELEGRAM_MESSAGE_LIMIT for part in result.response_parts)
    assert reply_messages(result) == result.response_parts

    sent: list[str] = []
    gateway.send_message = lambda chat_id, text, buttons=None, parse_mode="HTML": sent.append(text) or True  # type: ignore[method-assign]
    gateway.deliver_reply(10, result)
    assert sent == result.response_parts

    help_text = gateway.process_update(_message(2, "/help")).response_text
    assert "/tickets" in help_text
    assert "Dark Factory Autonomous Orchestrator Bot" in help_text


def test_tickets_command_delivers_every_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(f"USR-{index}", "planned", f"Titulo {index}", "d" * 200) for index in range(1, 25)]
    rows.append(_row("USR-90", "completed", "Feito", "MARCA_CONCLUIDA_189"))
    monkeypatch.setattr(
        "core.integrations.telegram_tickets.load_open_tickets",
        lambda path=None: rows,
    )

    gateway = _gateway(tmp_path)
    result = gateway.process_update(_message(3, "/tickets"))
    assert result.action == TelegramActionType.TICKETS
    assert len(result.response_parts) >= 2
    assert all(telegram_units(part) < TELEGRAM_MESSAGE_LIMIT for part in result.response_parts)
    joined = "\n".join(reply_messages(result))
    for index in range(1, 25):
        assert f"USR-{index}" in joined
    assert "MARCA_CONCLUIDA_189" not in joined

    sent: list[tuple[int, str]] = []

    def _capture(chat_id: int, text: str, buttons: object = None, parse_mode: str = "HTML") -> bool:
        sent.append((chat_id, text))
        return True

    gateway.send_message = _capture  # type: ignore[method-assign]
    gateway.deliver_reply(10, result)
    assert [text for _, text in sent] == result.response_parts
    assert all(chat_id == 10 for chat_id, _ in sent)


def test_owner_bot_does_not_list_tickets(tmp_path: Path) -> None:
    result = _gateway(tmp_path, role="owner").process_update(_message(4, "/tickets"))
    assert "@darkfac_ops_bot" in result.response_text
    assert result.action == TelegramActionType.UNKNOWN
    assert result.response_parts == []


def test_tickets_command_reports_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(path: object = None) -> list[object]:
        raise OSError("fila indisponivel")

    monkeypatch.setattr("core.integrations.telegram_tickets.load_open_tickets", _boom)
    result = _gateway(tmp_path).process_update(_message(5, "/tickets"))
    assert result.action == TelegramActionType.TICKETS
    assert result.error
    assert "Não foi possível listar" in result.response_text
    assert result.response_parts == []


def test_reply_messages_falls_back_to_single_text() -> None:
    single = TelegramDispatchResult(update_id=1, action=TelegramActionType.STATUS, authorized=True, response_text="ok")
    assert reply_messages(single) == ["ok"]
    empty = TelegramDispatchResult(update_id=2, action=TelegramActionType.UNKNOWN, authorized=True)
    assert reply_messages(empty) == []


def test_webhook_sends_each_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.backend.service import HubService

    rows = [_row(f"USR-{index}", "validating", f"Titulo {index}", "d" * 200) for index in range(1, 25)]
    monkeypatch.setattr("core.integrations.telegram_tickets.load_open_tickets", lambda path=None: rows)

    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hubdata")
    payload = _message(30, "/tickets")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "10")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "10")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    sent: list[str] = []

    def _capture(self: object, chat_id: int, text: str, buttons: object = None, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("core.integrations.telegram.TelegramGateway.send_message", _capture)
    body = service.process_telegram_webhook(payload)

    assert body["action"] == "tickets"
    assert len(body["response_parts"]) >= 2
    assert sent == body["response_parts"]
    assert all(telegram_units(part) < TELEGRAM_MESSAGE_LIMIT for part in sent)
    assert all(f"USR-{index}" in "\n".join(sent) for index in range(1, 25))
