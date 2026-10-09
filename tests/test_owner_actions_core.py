"""Owner action backlog: schema, tolerant store, CLI, HumanRequest mirror (USR-190)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.human import HumanRequest
from core.owner_actions import cli
from core.owner_actions.mirror import (
    close_mirrored_request,
    human_request_to_action,
    mirror_human_request,
    mirrored_id,
    parse_guide,
)
from core.owner_actions.models import (
    ActionKind,
    ActionPriority,
    ActionStatus,
    OwnerAction,
    OwnerActionDraft,
    find_secrets,
    redact,
)
from core.owner_actions.notify import build_message, notify_owner_action
from core.owner_actions.propagate import annotate_blocked_tickets
from core.owner_actions.store import (
    OwnerActionInvalid,
    OwnerActionNotFound,
    OwnerActionStore,
    OwnerActionStoreCorrupt,
    blocked_by,
    unblocks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = REPO_ROOT / ".factory" / "owner_actions" / "owner_actions.json"


def _draft(**overrides: Any) -> OwnerActionDraft:
    base: dict[str, Any] = {
        "title": "Fazer algo que so o owner pode",
        "priority": "high",
        "why": "Porque sim",
        "blocks": ["USR-1"],
        "steps": [{"text": "Abra a tela", "command": "echo ok"}],
        "verify": "Funciona",
    }
    base.update(overrides)
    return OwnerActionDraft.model_validate(base)


def _decision_draft(**overrides: Any) -> OwnerActionDraft:
    return _draft(
        kind="decision",
        steps=[],
        options=[{"id": "A", "label": "Opcao A", "detail": "d"}, {"id": "B", "label": "Opcao B"}],
        **overrides,
    )


@pytest.fixture
def store(tmp_path: Path) -> OwnerActionStore:
    return OwnerActionStore(tmp_path / "oa" / "owner_actions.json", overlay_path=tmp_path / "oa" / "resolutions.json")


# ------------------------------------------------------------------ schema


def test_seeded_backlog_is_valid_and_complete() -> None:
    """The curated file in the repo must load without a single warning."""
    result = OwnerActionStore(SEED_FILE).load()
    assert result.warnings == []
    assert result.actions, "seeded backlog must not be empty"
    ids = [a.id for a in result.actions]
    assert len(ids) == len(set(ids))
    for action in result.actions:
        if action.kind is ActionKind.DECISION:
            assert len(action.options) >= 2
        else:
            assert action.steps, f"{action.id} has no steps"


def test_action_requires_steps_and_decision_requires_two_options() -> None:
    with pytest.raises(ValueError):
        OwnerAction(id="OA-001", title="Sem passos", kind=ActionKind.ACTION)
    with pytest.raises(ValueError):
        OwnerAction(id="OA-001", title="Uma opcao", kind=ActionKind.DECISION, options=[{"id": "A", "label": "x"}])
    with pytest.raises(ValueError):
        OwnerAction(
            id="OA-001", title="Ids repetidos", kind=ActionKind.DECISION,
            options=[{"id": "A", "label": "x"}, {"id": "A", "label": "y"}],
        )


def test_invalid_id_and_self_dependency_rejected() -> None:
    step = [{"text": "x"}]
    with pytest.raises(ValueError):
        OwnerAction(id="X-1", title="Id ruim", steps=step)
    with pytest.raises(ValueError):
        OwnerAction(id="OA-001", title="Ciclo", steps=step, depends_on=["OA-001"])


def test_resolved_at_only_for_done_and_set_automatically() -> None:
    step = [{"text": "x"}]
    done = OwnerAction(id="OA-001", title="Feito", steps=step, status="done")
    assert done.resolved_at is not None
    with pytest.raises(ValueError):
        OwnerAction(id="OA-002", title="Aberto com data", steps=step, resolved_at="2026-10-09T10:00:00Z")


def test_credential_shaped_content_is_rejected_but_placeholders_pass() -> None:
    token = "123456789:" + "A" * 35
    assert find_secrets(f"use {token} agora") == ["telegram-bot-token"]
    assert REDACTED_OK(redact(f"use {token} agora"))
    with pytest.raises(ValueError):
        OwnerAction(id="OA-001", title="Vaza segredo", steps=[{"text": "x", "command": f"set TOKEN={token}"}])
    ok = OwnerAction(
        id="OA-001",
        title="Placeholders sao ok",
        steps=[{"text": "x", "command": 'psql "postgresql://USUARIO:SENHA@host:5432/BANCO" <TOKEN_NOVO>'}],
    )
    assert ok.steps[0].command


def REDACTED_OK(text: str) -> bool:  # noqa: N802 - tiny helper, keeps the assertion readable
    return "[REDACTED]" in text and "AAAA" not in text


# ------------------------------------------------------------------- store


def test_missing_file_is_empty_without_warning(store: OwnerActionStore) -> None:
    result = store.load()
    assert result.actions == [] and result.warnings == []
    assert store.next_id() == "OA-001"


@pytest.mark.parametrize("content", ["{not json", "\x00\x01garbage", "[1, 2, 3]", '{"actions": "nope"}'])
def test_corrupted_file_yields_empty_list_not_an_exception(store: OwnerActionStore, content: str) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    result = store.load()
    assert result.actions == []
    assert store.list_actions() == []


def test_corrupted_file_reports_warning_and_write_refuses_to_overwrite(store: OwnerActionStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")
    assert store.load().warnings and "corrompido" in store.load().warnings[0]
    with pytest.raises(OwnerActionStoreCorrupt):
        store.add(_draft())
    assert store.path.read_text(encoding="utf-8") == "{broken"


def test_invalid_row_is_skipped_with_warning_and_kept_on_rewrite(store: OwnerActionStore) -> None:
    good = _draft().build("OA-001").to_record()
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"schema_version": 1, "description": "d", "actions": [good, {"id": "OA-002", "title": "x"}]}),
        encoding="utf-8",
    )
    result = store.load()
    assert [a.id for a in result.actions] == ["OA-001"]
    assert any("OA-002" in w for w in result.warnings)
    created = store.add(_draft())
    assert created.id == "OA-003"  # the skipped row's id stays reserved
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert [r["id"] for r in document["actions"]] == ["OA-001", "OA-002", "OA-003"]
    assert document["description"] == "d"


def test_add_assigns_sequential_ids_and_writes_atomically(store: OwnerActionStore) -> None:
    first = store.add(_draft())
    second = store.add(_draft(title="Outra acao do owner"))
    assert (first.id, second.id) == ("OA-001", "OA-002")
    assert not list(store.path.parent.glob("*.tmp"))
    assert not list(store.path.parent.glob("*.lock"))
    assert [a.id for a in store.list_actions()] == ["OA-001", "OA-002"]


def test_add_rejects_invalid_and_secret_drafts(store: OwnerActionStore) -> None:
    with pytest.raises(OwnerActionInvalid):
        store.add(_draft(steps=[]))
    with pytest.raises(OwnerActionInvalid) as caught:
        store.add(_draft(steps=[{"text": "cole ghp_" + "a" * 36}]))
    assert "a" * 36 not in str(caught.value)
    assert store.list_actions() == []


def test_resolve_action_sets_resolved_at_and_is_idempotent(store: OwnerActionStore) -> None:
    created = store.add(_draft())
    done = store.resolve(created.id)
    assert done.status is ActionStatus.DONE and done.resolved_at is not None
    again = store.resolve(created.id)
    assert again.resolved_at == done.resolved_at
    with pytest.raises(OwnerActionNotFound):
        store.resolve("OA-404")
    with pytest.raises(OwnerActionInvalid):
        store.resolve(created.id, option_id="A")


def test_resolve_decision_requires_valid_option_and_keeps_first_answer(store: OwnerActionStore) -> None:
    created = store.add(_decision_draft())
    with pytest.raises(OwnerActionInvalid):
        store.resolve(created.id)
    with pytest.raises(OwnerActionInvalid):
        store.resolve(created.id, option_id="Z")
    answered = store.resolve(created.id, option_id="B", note=" prefiro B ")
    assert answered.answer is not None and answered.answer.option_id == "B" and answered.answer.note == "prefiro B"
    assert store.resolve(created.id, option_id="B").answer.option_id == "B"
    with pytest.raises(OwnerActionInvalid):
        store.resolve(created.id, option_id="A")


def test_overlay_resolution_wins_over_open_definition_and_survives_reload(tmp_path: Path) -> None:
    definitions = tmp_path / "defs" / "owner_actions.json"
    overlay = tmp_path / "volume" / "resolutions.json"
    agent_store = OwnerActionStore(definitions)
    action = agent_store.add(_draft())
    decision = agent_store.add(_decision_draft(title="Decidir algo importante"))

    hub_store = OwnerActionStore(definitions, overlay_path=overlay)
    hub_store.resolve(action.id, to_overlay=True)
    hub_store.resolve(decision.id, option_id="A", note="n", to_overlay=True)

    # A fresh process (redeploy) sees the closed items even though the definitions still say open.
    reloaded = OwnerActionStore(definitions, overlay_path=overlay)
    by_id = {a.id: a for a in reloaded.load().actions}
    assert by_id[action.id].status is ActionStatus.DONE
    assert by_id[decision.id].answer.option_id == "A"
    assert json.loads(definitions.read_text(encoding="utf-8"))["actions"][0]["status"] == "open"


def test_corrupted_overlay_is_ignored_on_read_and_replaced_on_write(tmp_path: Path) -> None:
    definitions = tmp_path / "owner_actions.json"
    overlay = tmp_path / "resolutions.json"
    store = OwnerActionStore(definitions, overlay_path=overlay)
    created = store.add(_draft())
    overlay.write_text("{oops", encoding="utf-8")
    loaded = store.load()
    assert loaded.actions[0].status is ActionStatus.OPEN and loaded.warnings
    store.resolve(created.id, to_overlay=True)
    assert store.get(created.id).status is ActionStatus.DONE
    assert list(tmp_path.glob("resolutions.json.corrupt-*"))


def test_dependency_helpers(store: OwnerActionStore) -> None:
    first = store.add(_draft())
    second = store.add(_draft(title="Depende da primeira", depends_on=[first.id]))
    third = store.add(_draft(title="Depende de algo inexistente", depends_on=["OA-999"]))
    actions = store.list_actions()
    by_id = {a.id: a for a in actions}
    assert blocked_by(by_id[second.id], by_id) == [first.id]
    assert blocked_by(by_id[third.id], by_id) == ["OA-999"]
    assert unblocks(by_id[first.id], actions) == [second.id]
    store.resolve(first.id)
    actions = store.list_actions()
    by_id = {a.id: a for a in actions}
    assert blocked_by(by_id[second.id], by_id) == []
    assert unblocks(by_id[first.id], actions) == []


# --------------------------------------------------------- decision -> tickets


def _ticket_store(tmp_path: Path) -> DemandsStore:
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(UserTicket(id="USR-1", title="Ticket bloqueado", problem_statement="Contexto original"))
    demands.save_ticket(UserTicket(id="USR-2", title="Ticket outro", problem_statement="Outro"))
    return demands


def test_answer_is_attached_to_blocked_tickets_idempotently(store: OwnerActionStore, tmp_path: Path) -> None:
    demands = _ticket_store(tmp_path)
    created = store.add(_decision_draft(blocks=["USR-1", "USR-404"]))
    answered = store.resolve(created.id, option_id="A", note="vai de A")
    assert annotate_blocked_tickets(answered, demands) == ["USR-1"]
    ticket = demands.get_ticket("USR-1")
    assert f"owner-decision:{created.id}=A" in ticket.tags
    assert ticket.problem_statement.startswith("Contexto original")
    assert "vai de A" in ticket.problem_statement
    assert demands.get_ticket("USR-2").tags == ["user-demand"]
    # Answering again must replace, not stack.
    annotate_blocked_tickets(answered, demands)
    again = demands.get_ticket("USR-1")
    assert again.problem_statement.count("[Decisao do owner") == 1
    assert [t for t in again.tags if t.startswith("owner-decision:")] == [f"owner-decision:{created.id}=A"]


def test_plain_actions_do_not_touch_tickets(store: OwnerActionStore, tmp_path: Path) -> None:
    demands = _ticket_store(tmp_path)
    done = store.resolve(store.add(_draft(blocks=["USR-1"])).id)
    assert annotate_blocked_tickets(done, demands) == []
    assert demands.get_ticket("USR-1").tags == ["user-demand"]


# ------------------------------------------------------------------ notify


def test_notify_only_critical_and_high_and_never_raises(store: OwnerActionStore) -> None:
    sent: list[tuple[str, Any]] = []

    def sender(text: str, buttons: Any) -> bool:
        sent.append((text, buttons))
        return True

    high = store.add(_draft(priority="high"))
    low = store.add(_draft(priority="low", title="Item de baixa prioridade"))
    assert notify_owner_action(high, sender=sender, hub_base_url="https://hub.test")["sent"] is True
    assert "https://hub.test/#owner-actions" in sent[0][0]
    assert notify_owner_action(low, sender=sender)["attempted"] is False
    assert len(sent) == 1

    def boom(text: str, buttons: Any) -> bool:
        raise RuntimeError("telegram caiu")

    assert notify_owner_action(high, sender=boom) == {
        "attempted": True, "sent": False, "link": "https://darkhub.ggcampos.com/#owner-actions",
    }
    assert "OA-" in build_message(high, "https://x/#owner-actions")


def test_notify_reuses_telegram_gateway_and_survives_missing_token(monkeypatch: pytest.MonkeyPatch, store: OwnerActionStore) -> None:
    from unittest.mock import patch

    for name in ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_OWNER_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    high = store.add(_draft(priority="critical"))
    with patch("core.integrations.telegram.TelegramGateway.send_message", return_value=True) as send:
        result = notify_owner_action(high)  # no token configured -> nothing goes on the wire
        assert result["attempted"] is True and result["sent"] is False
        send.assert_not_called()
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "unit-test-fake-token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "111")
    with patch("core.integrations.telegram.TelegramGateway.send_message", return_value=True) as send:
        result = notify_owner_action(high, hub_base_url="https://darkhub.test")
        assert result["sent"] is True
        text = send.call_args.kwargs["text"]
        assert high.id in text and "https://darkhub.test/#owner-actions" in text


# --------------------------------------------------------------------- CLI


def _run(argv: list[str], store: OwnerActionStore, **kwargs: Any) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.run(argv, store=store, out=out, **kwargs)
    return code, out.getvalue()


def test_cli_add_list_done_answer_roundtrip(store: OwnerActionStore, tmp_path: Path) -> None:
    notified: list[str] = []
    code, out = _run(
        [
            "add", "--title", "Colocar o token novo", "--priority", "high", "--why", "Token revogado",
            "--blocks", "USR-1", "--step", "Abra o painel", "--cmd", "echo um", "--cmd", "echo dois",
            "--step", "Salve", "--verify", "Bot responde",
        ],
        store,
        notifier=lambda a: notified.append(a.id) or {"sent": True},
    )
    assert code == 0 and "OA-001" in out and notified == ["OA-001"]
    action = store.get("OA-001")
    assert [s.text for s in action.steps] == ["Abra o painel", "Salve"]
    assert action.steps[0].command == "echo um\necho dois" and action.steps[1].command is None

    code, out = _run(
        ["add", "--kind", "decision", "--title", "Qual retencao usar", "--priority", "medium",
         "--blocks", "USR-1", "--option", "A=Catorze dias|recomendado", "--option", "B=Sete dias"],
        store,
        notifier=lambda a: notified.append(a.id) or {"sent": True},
    )
    assert code == 0 and "OA-002" in out and notified == ["OA-001"]  # medium never notifies

    code, out = _run(["list"], store)
    assert "OA-001" in out and "OA-002" in out
    code, out = _run(["list", "--priority", "high", "--json"], store)
    assert [r["id"] for r in json.loads(out)] == ["OA-001"]

    assert _run(["done", "OA-001"], store)[0] == 0
    assert "OA-001" not in _run(["list"], store)[1]
    assert "OA-001" in _run(["list", "--status", "done"], store)[1]

    demands = _ticket_store(tmp_path)
    code, out = _run(["answer", "OA-002", "--option", "A", "--note", "ok"], store, demands_store=demands)
    assert code == 0 and "USR-1" in out
    assert "owner-decision:OA-002=A" in demands.get_ticket("USR-1").tags


def test_cli_errors_exit_nonzero_without_leaking_secrets(store: OwnerActionStore, capsys: pytest.CaptureFixture[str]) -> None:
    token = "ghp_" + "b" * 36
    code, _ = _run(["add", "--title", "Titulo valido", "--step", f"use {token}"], store, notifier=lambda a: {"sent": False})
    assert code == 1
    captured = capsys.readouterr()
    assert token not in captured.err and token not in captured.out
    assert _run(["done", "OA-404"], store)[0] == 1
    assert _run(["add", "--title", "Sem passos"], store, notifier=lambda a: {})[0] == 1
    assert _run(["add", "--title", "Cmd solto", "--cmd", "x"], store, notifier=lambda a: {})[0] == 1
    with pytest.raises(SystemExit):
        _run(["add", "--title", "Prioridade invalida", "--priority", "urgentissima"], store)


def test_cli_notification_failure_never_fails_the_command(store: OwnerActionStore) -> None:
    def boom(action: OwnerAction) -> dict[str, Any]:
        raise RuntimeError("sem rede")

    code, out = _run(["add", "--title", "Item critico", "--priority", "critical", "--step", "Passo"], store, notifier=boom)
    assert code == 0 and store.get("OA-001") is not None and "Telegram nao enviou" in out


def test_cli_from_json_and_list_tolerates_corruption(store: OwnerActionStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    draft_file = tmp_path / "draft.json"
    draft_file.write_text(_draft().model_dump_json(), encoding="utf-8")
    assert _run(["add", "--title", "ignorado", "--from-json", str(draft_file), "--no-notify"], store)[0] == 0
    store.path.write_text("{corrupted", encoding="utf-8")
    code, out = _run(["list"], store)
    assert code == 0 and "Nenhum item" in out
    assert "AVISO" in capsys.readouterr().err


def test_launcher_script_runs_standalone(tmp_path: Path) -> None:
    env = {**__import__("os").environ, "DARKFAC_OWNER_ACTIONS_PATH": str(tmp_path / "oa.json"), "PYTHONUTF8": "1"}
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "owner_action.py"), "list"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp_path, timeout=60,
    )
    assert done.returncode == 0 and "Nenhum item" in done.stdout


# --------------------------------------------------------- HumanRequest mirror

GUIDE = """Preciso do token do GitHub para o repositorio.

1. Abra https://github.com/settings/tokens e clique em **Generate new token**.
2. Defina o escopo `repo` e confirme.
```
gh auth status
```
3. Cole o token no cofre.
"""


def _request(**kw: Any) -> HumanRequest:
    base: dict[str, Any] = {"kind": "secret", "run_id": "run-darkfac-USR-9", "blocking_stage": "build", "guide_md": GUIDE}
    base.update(kw)
    return HumanRequest(**base)


def test_parse_guide_extracts_steps_and_commands() -> None:
    intro, steps = parse_guide(GUIDE)
    assert intro.startswith("Preciso do token")
    assert [s.text.split(" ")[0] for s in steps] == ["Abra", "Defina", "Cole"]
    assert steps[1].command == "gh auth status"
    assert "**" not in steps[0].text
    assert parse_guide("Apenas um texto sem lista")[1][0].text == "Apenas um texto sem lista"


def test_human_request_becomes_owner_action_with_its_steps() -> None:
    action = human_request_to_action(_request(probe_cmd="gh auth status"))
    assert action.id == mirrored_id("run-darkfac-USR-9", "build") == "OA-HR-run-darkfac-USR-9-build"
    assert action.kind is ActionKind.ACTION and action.priority is ActionPriority.HIGH
    assert len(action.steps) == 3 and "gh auth status" in action.verify
    assert action.created_by == "production-line"
    assert _request(kind="grill").kind == "grill"
    assert human_request_to_action(_request(kind="grill")).priority is ActionPriority.MEDIUM


def test_mirror_is_idempotent_and_closed_when_the_line_resumes(store: OwnerActionStore) -> None:
    request = _request()
    first = mirror_human_request(request, store=store)
    second = mirror_human_request(request, store=store)
    assert first is not None and second is not None and first.id == second.id
    assert len(store.list_actions()) == 1
    assert close_mirrored_request(request.run_id, request.blocking_stage, store=store) is True
    assert close_mirrored_request(request.run_id, request.blocking_stage, store=store) is False
    assert store.get(first.id).status is ActionStatus.DONE
    # Re-mirroring a closed request must not reopen it.
    mirror_human_request(request, store=store)
    assert store.get(first.id).status is ActionStatus.DONE


def test_mirror_never_raises(tmp_path: Path) -> None:
    bad = OwnerActionStore(tmp_path / "bad.json")
    bad.path.write_text("{corrupted", encoding="utf-8")
    assert mirror_human_request(_request(), store=bad) is None
    assert mirror_human_request(object(), store=bad) is None


def test_request_human_help_mirrors_via_env_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The line hook writes to the backlog configured by DARKFAC_OWNER_ACTIONS_PATH (best effort)."""
    from core.line import human

    target = tmp_path / "oa.json"
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(target))
    monkeypatch.setattr(human, "save_request", lambda project, request: tmp_path / "HUMAN_REQUEST.json")
    sent: list[str] = []
    human.request_human_help(object(), _request(), send=lambda text: sent.append(text) or True)  # type: ignore[arg-type]
    assert sent, "owner bot notification is unchanged"
    assert [a.id for a in OwnerActionStore(target).list_actions()] == ["OA-HR-run-darkfac-USR-9-build"]

    class _Store:
        def find_job(self, run_id: str, stage: str, status: str | None = None) -> object | None:
            return object() if status == "waiting_human" else None

        def resume_job(self, key: object, now: object) -> bool:
            return True

    assert human.resume_blocked_job(_Store(), "run-darkfac-USR-9", "build") is True  # type: ignore[arg-type]
    assert OwnerActionStore(target).list_actions()[0].status is ActionStatus.DONE
