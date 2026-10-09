"""USR-197: /status do Telegram reflete o estado real da fabrica (hermetico, sem rede)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.integrations.telegram_status import (
    UNAVAILABLE,
    build_status_message,
    read_convergence_snapshot,
)

NOW = datetime(2026, 10, 10, 12, 30, 0, tzinfo=timezone.utc)


def _ticket(tid: str, status: str, *, title: str = "t", horizon: str = "now", created: str = "2026-10-01T00:00:00+00:00", project: str = "darkfac"):
    return SimpleNamespace(
        id=tid,
        title=title or tid,
        project_id=project,
        status=SimpleNamespace(value=status),
        horizon=SimpleNamespace(value=horizon),
        created_at=datetime.fromisoformat(created),
    )


def _actions() -> list[dict[str, str]]:
    return [
        {"id": "OA-008", "title": "Destravar o Desktop", "priority": "critical"},
        {"id": "OA-010", "title": "Rotacionar token", "priority": "high"},
        {"id": "OA-011", "title": "Revisar <script>", "priority": "medium"},
        {"id": "OA-012", "title": "Quarta acao", "priority": "low"},
    ]


def _build(**overrides):
    kwargs = dict(
        owner_actions=_actions,
        tickets=lambda: [
            _ticket("USR-1", "completed"),
            _ticket("USR-2", "completed"),
            _ticket("USR-3", "planned", title="Mais velho", horizon="next", created="2026-09-01T00:00:00+00:00"),
            _ticket("USR-4", "planned", title="Prioritario", horizon="now", created="2026-10-05T00:00:00+00:00"),
            _ticket("USR-5", "planned", title="Prioritario antigo", horizon="now", created="2026-10-02T00:00:00+00:00"),
            _ticket("USR-6", "implementing"),
            _ticket("USR-7", "validating"),
            _ticket("USR-AUTO", "completed", title="Bug em inicializacao do terminal"),
            _ticket("USR-9", "planned", project="jarvis"),
        ],
        runs=lambda: [
            {"ticket_id": "USR-6", "title": "Em dev", "stage": "Desenvolvimento", "state": "running", "waiting_human": False},
            {"ticket_id": "USR-8", "title": "Pergunta", "stage": "Grill", "state": "attention", "waiting_human": True},
        ],
        convergence=lambda: {"nodes": {"Notebook": 0, "Desktop": 2, "VPS": 0}, "age_seconds": 7200, "pending": None},
        now=NOW,
        hub_url="https://darkhub.ggcampos.com",
    )
    kwargs.update(overrides)
    return build_status_message(**kwargs)


def test_status_has_all_blocks_with_real_data() -> None:
    text = _build()
    assert "4 acoes abertas" in text
    assert "1 critical" in text and "1 high" in text
    assert "OA-008" in text and "OA-010" in text and "OA-011" in text
    assert "OA-012" not in text  # apenas os 3 primeiros
    assert "https://darkhub.ggcampos.com/#owner-actions" in text
    # sintetico USR-AUTO e outro projeto descartados
    assert "USR-AUTO" not in text
    assert "completed 2" in text
    assert "planned 3" in text
    assert "em andamento 2" in text
    assert "aguardando humano 1" in text
    # proximo: horizonte now e mais antigo
    assert "USR-5" in text and "Prioritario antigo" in text
    # runs e nos
    assert "USR-6" in text and "Desenvolvimento" in text
    assert "Desktop" in text and "divergente" in text
    assert "2026-10-10 12:30:00 UTC" in text
    assert "Pipeline da Dark Factory ativo e operacional" not in text


def test_status_escapes_html_in_titles() -> None:
    text = _build()
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_status_blocks_fail_in_isolation() -> None:
    def boom():
        raise RuntimeError("falha interna secreta")

    text = _build(owner_actions=boom)
    assert UNAVAILABLE in text
    assert "falha interna secreta" not in text
    assert "USR-5" in text  # tickets continuam

    # runs also feeds the "aguardando humano" counter, so its failure shows up twice
    for name, expected in (("tickets", 1), ("runs", 2), ("convergence", 1)):
        text = _build(**{name: boom})
        assert text.count(UNAVAILABLE) == expected
        assert "OA-008" in text


def test_status_fits_telegram_limit_and_balances_tags() -> None:
    many = [{"id": f"OA-{i:03d}", "title": "x" * 500, "priority": "high"} for i in range(1, 50)]
    runs = [
        {"ticket_id": f"USR-{i}", "title": "y" * 500, "stage": "z" * 200, "state": "running", "waiting_human": False}
        for i in range(1, 80)
    ]
    text = _build(owner_actions=lambda: many, runs=lambda: runs)
    assert len(text) <= 3800
    assert text.count("<b>") == text.count("</b>")


def test_status_without_tickets_or_actions() -> None:
    text = _build(owner_actions=lambda: [], tickets=lambda: [], runs=lambda: [], convergence=lambda: {"nodes": {}, "age_seconds": None, "pending": None})
    assert "0 acoes" in text or "nenhuma acao" in text.lower()
    assert "USR-AUTO" not in text


def test_status_hub_url_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKHUB_BASE_URL", raising=False)
    assert "https://darkhub.ggcampos.com/#owner-actions" in _build(hub_url=None)
    monkeypatch.setenv("DARKHUB_BASE_URL", "http://hub.local:9000/")
    assert "http://hub.local:9000/#owner-actions" in _build(hub_url=None)


def test_read_convergence_snapshot_from_state_files(tmp_path: Path) -> None:
    (tmp_path / "node_sync_divergence.json").write_text(json.dumps({"Notebook": 0, "Desktop": 3}), encoding="utf-8")
    (tmp_path / "node_sync_pending.json").write_text(
        json.dumps({"node": "Desktop", "reason": "aguardando restart", "expected_sha": "a" * 40}), encoding="utf-8"
    )
    snap = read_convergence_snapshot(tmp_path, now=datetime.now(timezone.utc))
    assert snap["nodes"] == {"Notebook": 0, "Desktop": 3}
    assert snap["pending"]["node"] == "Desktop"
    assert snap["age_seconds"] is not None and snap["age_seconds"] >= 0


def test_read_convergence_snapshot_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_convergence_snapshot(tmp_path)


def test_hub_status_handler_uses_real_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.backend.service import HubService

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    service = HubService(project_root=tmp_path)
    service.list_demand_tickets = mock.MagicMock(
        return_value=[
            _ticket("USR-AUTO", "completed", title="Bug em inicializacao do terminal"),
            _ticket("USR-5", "planned", title="Real"),
        ]
    )
    summary = service.telegram_status_summary()
    assert "Pipeline da Dark Factory ativo e operacional" not in summary
    assert "USR-AUTO" not in summary
    assert "USR-5" in summary
    assert "UTC" in summary
    assert f"<b>Nos</b>: {UNAVAILABLE}" in summary  # no deploy cycle recorded in the hermetic state root

    gw = service._build_telegram_gateway()
    assert gw.status_handler is not None
    assert "USR-5" in gw.status_handler(None)["summary"]

    service.get_demand_ticket = mock.MagicMock(return_value=_ticket("USR-5", "planned", title="Real"))
    assert gw.status_handler("USR-5")["summary"].startswith("Ticket USR-5: status=planned")
    service.get_demand_ticket = mock.MagicMock(return_value=None)
    assert "nao encontrado" in gw.status_handler("USR-404")["summary"].lower().replace("ã", "a")
