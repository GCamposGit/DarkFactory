from datetime import datetime, timezone

from core.line.fleet_dispatcher import DispatcherConfig, FleetDispatcher
from core.notifications import daily_digest as dd

NOW = datetime(2026, 10, 25, 11, 30, tzinfo=timezone.utc)  # 08:30 em Sao Paulo


def _sources() -> dd.DigestSources:
    return dd.DigestSources(
        planned=[{"id": "USR-1", "title": "Primeiro", "status": "planned", "tags": ["bloco-a"], "dependencies": []}],
        in_flight={"notebook": "USR-9"},
        owner_actions=[
            {"id": "OA-2", "title": "Baixa", "priority": "low", "kind": "action", "blocks": []},
            {"id": "OA-1", "title": "Escolher rota", "priority": "critical", "kind": "decision", "blocks": ["USR-1"]},
        ],
        deliveries=[{
            "ticket": "USR-5", "title": "Feito", "pr_url": "https://github.com/o/r/pull/7",
            "pack_url": "https://darkhub.ggcampos.com/api/learning-packs/p1/html",
            "manual_pages": ["docs/manual/fila.md"],
        }],
    )


def test_morning_lists_pending_decisions_by_priority() -> None:
    text = dd.build_morning(NOW, _sources())
    assert text.index("OA-1") < text.index("OA-2")
    assert "CRITICAL OA-1 (decisao)" in text and "bloqueia USR-1" in text
    assert "USR-1" in text and "USR-9 (notebook)" in text


def test_evening_has_pr_pack_and_manual_links() -> None:
    text = dd.build_evening(NOW, _sources())
    assert "https://github.com/o/r/pull/7" in text
    assert "/api/learning-packs/p1/html" in text
    assert "docs/manual/fila.md" in text


def test_message_respects_telegram_limit() -> None:
    src = _sources()
    src.owner_actions = [
        {"id": f"OA-{i}", "title": "x" * 300, "priority": "high", "kind": "action", "blocks": []} for i in range(300)
    ]
    src.deliveries = [{"ticket": f"USR-{i}", "title": "y" * 300, "pr_url": "https://x/" + "z" * 200} for i in range(100)]
    assert len(dd.build_morning(NOW, src)) <= dd.TELEGRAM_LIMIT
    assert len(dd.build_evening(NOW, src)) <= dd.TELEGRAM_LIMIT


def test_dispatcher_hook_sends_once_per_day(tmp_path) -> None:
    sent: list[str] = []
    state = tmp_path / "state.json"

    def hook(now: datetime):
        return dd.maybe_send_due(now, send_fn=lambda k: sent.append(k) or True, state_path=state)

    d = FleetDispatcher(
        DispatcherConfig(hosts=[], state_path=tmp_path / "d.json"), demands_loader=list, digest_hook=lambda: hook(NOW)
    )
    d.run_digest_hook()
    d.run_digest_hook()
    assert sent == ["morning"]
    evening = datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc)  # 21:30 em Sao Paulo
    assert hook(evening) == "evening" and hook(evening) is None
