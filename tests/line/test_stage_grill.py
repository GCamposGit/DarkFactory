"""Tests for the single-round Grill stage (HF-27-04).

The agent CLI itself is faked by monkeypatching `stage_grill.run_read_agent`
(the same seam `stage_planning` shares) so these tests focus on the Grill
stage's own contract: technical questions resolved immediately, a single
bundled Telegram-style message for blocking questions, the 12h
default-after-timeout rule, `secret`/`account` never getting a default, and
idempotent replay via the `DarkFac-Job:` trailer. No network and no real git
remote beyond a local `git init --bare` "origin" under `tmp_path`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line import stage_grill
from core.line.agent_cli import AgentResult
from core.line.stage_grill import run_grill, submit_grill_answers
from core.projects.models import ProjectDescriptor


# --------------------------------------------------------------------------
# Local git fixtures (self-contained; no network)
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


def _project(repo_url: str) -> ProjectDescriptor:
    return ProjectDescriptor(id="acme", name="Acme Project", repo_url=repo_url)


@pytest.fixture()
def project(tmp_path, monkeypatch) -> ProjectDescriptor:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    return _project(str(origin))


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """Never reach a real owner bot, even on hosts with Telegram configured."""
    monkeypatch.setattr(stage_grill, "default_telegram_sender", lambda: None)


def _fake_agent_reply(payload: dict) -> AgentResult:
    return AgentResult(ok=True, text=json.dumps(payload), harness="claude", duration_s=0.01)


# --------------------------------------------------------------------------
# No blocking questions -> immediate success
# --------------------------------------------------------------------------


def test_no_blocking_questions_immediate_success(project, monkeypatch):
    reply = _fake_agent_reply(
        {
            "questions": [
                {
                    "id": "q1",
                    "text": "Qual framework de testes usar?",
                    "kind": "technical",
                    "options": ["pytest", "unittest"],
                    "recommended": "pytest",
                    "why": "ja usado no repo",
                }
            ],
            "assumptions": ["Usar Python 3.12"],
            "is_product_scale": False,
        }
    )
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: reply)
    sent = []
    result = run_grill(
        project, "run-1", "Adicionar endpoint de health-check",
        send_message=lambda text, buttons: sent.append((text, buttons)),
    )

    assert result.outcome == "success"
    assert result.output_refs

    from core.line import workspace as ws_mod

    ws = ws_mod.checkout(project, "run-1")
    grill_md = (ws_mod.context_dir(ws) / "GRILL.md").read_text(encoding="utf-8")
    assert "Usar Python 3.12" in grill_md
    assert "pytest" in grill_md
    assert "Aguardando decisao" not in grill_md
    assert not sent  # no Telegram message when nothing blocks


def test_no_blocking_questions_is_idempotent_on_replay(project, monkeypatch):
    reply = _fake_agent_reply({"questions": [], "assumptions": ["ok"], "is_product_scale": False})
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        calls["n"] += 1
        return reply

    monkeypatch.setattr(stage_grill, "run_read_agent", _fake)

    first = run_grill(project, "run-2", "Demanda simples")
    second = run_grill(project, "run-2", "Demanda simples")

    assert first.outcome == "success"
    assert second.outcome == "success"
    assert first.output_refs == second.output_refs
    assert calls["n"] == 1  # the agent is never invoked again once the grill commit exists


# --------------------------------------------------------------------------
# Blocking business question -> waiting_human -> default after timeout
# --------------------------------------------------------------------------


def test_business_question_waits_then_defaults_after_timeout(project, monkeypatch):
    reply = _fake_agent_reply(
        {
            "questions": [
                {
                    "id": "q1",
                    "text": "Deve cobrar assinatura mensal?",
                    "kind": "business",
                    "options": ["sim", "nao"],
                    "recommended": "nao",
                    "why": "MVP gratuito",
                }
            ],
            "assumptions": [],
            "is_product_scale": False,
        }
    )
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: reply)
    sent = []
    start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    first = run_grill(
        project, "run-3", "Nova feature de billing",
        send_message=lambda text, buttons: sent.append((text, buttons)) or True,
        now=start,
    )
    assert first.outcome == "waiting_human"
    assert len(sent) == 1
    text, buttons = sent[0]
    assert "Deve cobrar assinatura mensal?" in text
    assert buttons and buttons[0][0]["callback_data"] == "cb:grill:run-3#q1:0"

    # Before the deadline and with no answer yet: still waiting, no new commit.
    still_waiting = run_grill(project, "run-3", "Nova feature de billing", now=start + timedelta(hours=1))
    assert still_waiting.outcome == "waiting_human"

    # After the 12h deadline with no answer: resolves with the recommended default.
    after_deadline = run_grill(
        project, "run-3", "Nova feature de billing", now=start + timedelta(hours=13)
    )
    assert after_deadline.outcome == "success"

    from core.line import workspace as ws_mod

    ws = ws_mod.checkout(project, "run-3")
    grill_md = (ws_mod.context_dir(ws) / "GRILL.md").read_text(encoding="utf-8")
    assert "default_after_timeout" in grill_md
    assert "nao" in grill_md


def test_answers_submitted_before_deadline_finalize_immediately(project, monkeypatch):
    reply = _fake_agent_reply(
        {
            "questions": [
                {
                    "id": "q1",
                    "text": "Qual paleta de cores usar?",
                    "kind": "intent",
                    "options": ["azul", "verde"],
                    "recommended": "azul",
                }
            ],
            "assumptions": [],
            "is_product_scale": False,
        }
    )
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: reply)
    start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    first = run_grill(project, "run-4", "Landing page nova", now=start)
    assert first.outcome == "waiting_human"

    # The Telegram button carries the option index ("1" -> "verde").
    submitted = submit_grill_answers(project, "run-4", {"q1": "1"})
    assert submitted is True

    resolved = run_grill(project, "run-4", "Landing page nova", now=start + timedelta(minutes=5))
    assert resolved.outcome == "success"

    from core.line import workspace as ws_mod

    ws = ws_mod.checkout(project, "run-4")
    grill_md = (ws_mod.context_dir(ws) / "GRILL.md").read_text(encoding="utf-8")
    assert "verde" in grill_md
    assert "respondida pelo owner" in grill_md
    assert "default_after_timeout" not in grill_md


# --------------------------------------------------------------------------
# secret/account questions never get a default
# --------------------------------------------------------------------------


def test_secret_question_never_receives_a_default(project, monkeypatch):
    reply = _fake_agent_reply(
        {
            "questions": [
                {
                    "id": "q1",
                    "text": "Qual a API key do provedor de pagamento?",
                    "kind": "secret",
                    "options": [],
                    "recommended": None,
                }
            ],
            "assumptions": [],
            "is_product_scale": False,
        }
    )
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: reply)
    start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    run_grill(project, "run-5", "Integrar pagamentos", now=start)
    resolved = run_grill(project, "run-5", "Integrar pagamentos", now=start + timedelta(hours=13))

    # The grill stage itself still completes (only dependent jobs block later, per HF-27-08).
    assert resolved.outcome == "success"
    assert any(ref.startswith("blocked_no_default:q1") for ref in resolved.evidence_refs)

    from core.line import workspace as ws_mod

    ws = ws_mod.checkout(project, "run-5")
    grill_md = (ws_mod.context_dir(ws) / "GRILL.md").read_text(encoding="utf-8")
    assert "default_after_timeout" not in grill_md
    assert "Aguardando decisao do owner" in grill_md


# --------------------------------------------------------------------------
# Review follow-ups: prompt rendering and durable owner notification
# --------------------------------------------------------------------------


def test_render_prompt_unescapes_braces_and_keeps_values_literal():
    rendered = stage_grill.render_prompt('{demand} -> {{"a":{{"b":1}}}} {unknown}', demand="x {grill} {{y}}")
    assert rendered == 'x {grill} {{y}} -> {"a":{"b":1}} {unknown}'


def test_shipped_prompts_render_valid_json_examples():
    for name in ("grill.md", "planning.md"):
        rendered = stage_grill.render_prompt(
            stage_grill.load_prompt(name), demand="d", grill="g", commands="c", lessons="l"
        )
        assert "{{" not in rendered and "}}" not in rendered
        example = rendered.split("```json", 1)[1].split("```", 1)[0]
        json.loads(example)


def test_failed_notification_is_retried_on_next_reconcile(project, monkeypatch):
    reply = _fake_agent_reply(
        {
            "questions": [
                {"id": "q1", "text": "Cobrar <assinatura> & taxa?", "kind": "business",
                 "options": ["sim", "nao"], "recommended": "nao"}
            ],
            "assumptions": [],
            "is_product_scale": False,
        }
    )
    monkeypatch.setattr(stage_grill, "run_read_agent", lambda *a, **k: reply)
    start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    attempts = []

    first = run_grill(project, "run-9", "x", send_message=lambda t, b: attempts.append(t) and False, now=start)
    assert first.outcome == "waiting_human"
    assert len(attempts) == 1
    assert "&lt;assinatura&gt; &amp; taxa" in attempts[0]

    ok = []
    again = run_grill(project, "run-9", "x", send_message=lambda t, b: ok.append(t) or True,
                      now=start + timedelta(hours=1))
    assert again.outcome == "waiting_human"
    assert len(ok) == 1

    third = run_grill(project, "run-9", "x", send_message=lambda t, b: ok.append(t) or True,
                      now=start + timedelta(hours=2))
    assert third.outcome == "waiting_human"
    assert len(ok) == 1  # already notified; not sent twice
