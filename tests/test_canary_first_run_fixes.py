"""Regression tests for the bugs found in the canary's first real production run.

1. `AgentResult(text=None)` crashed pydantic (an OpenRouter reasoning model
   returned `content: null`) -> classified `empty_output` retry instead.
2. Workers whose `harness:*` caps were all dropped by the auth probe still
   claimed agent stages (initial grill job has no required caps) -> classified
   `no_authenticated_harness` retry with a `not_before` delay.
3. `jobs.cause_code` is VARCHAR(64) on Postgres; long causes overflowed
   `finish()` -> normalized to <= 64 chars, and a failing `finish()` falls back
   to a short recorded failure instead of leaving the job `running`.
4. Codex device-auth bootstrap trigger (throttled stamp) + periodic re-probe.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from core.line import agent_cli, auth_bootstrap, stage_grill
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.bindings import LINE_STAGES, agent_route_unavailable
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_worker import CloudWorker, DEFAULT_CAPABILITIES
from core.workflow.control_contracts import (
    CAUSE_CODE_MAX_LEN,
    IntakeCommand,
    RuntimeOwner,
    StageContext,
    StageResult,
    normalize_cause_code,
)
from core.workflow.handlers import HandlerRegistry, build_handlers


# --------------------------------------------------------------------------
# 1. AgentResult / empty output
# --------------------------------------------------------------------------


def test_agent_result_none_text_is_empty_string_not_a_crash() -> None:
    result = AgentResult(ok=False, text=None, harness="openrouter", duration_s=0.1)  # type: ignore[arg-type]
    assert result.text == ""


class _NullContentResponse:
    text = None
    model = "some/reasoning-model"
    tokens_prompt = 10
    tokens_completion = 1024
    total_tokens = 1034
    measured_cost = None
    estimated_cost = 0.0
    is_measured = False


def test_openrouter_null_content_becomes_classified_empty_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _FakeProvider:
        def generate(self, prompt: str, **kwargs: Any) -> _NullContentResponse:
            captured.update(kwargs)
            return _NullContentResponse()

    import core.execution.providers as providers

    monkeypatch.setattr(providers, "OpenRouterModelProvider", _FakeProvider)
    result = run_agent(AgentRequest(prompt="grill", cwd=tmp_path, mode="read", harness="openrouter", model="m"))

    assert result.ok is False
    assert result.error_kind == "empty_output"
    assert result.text == ""
    # Reasoning models need more than the provider's 1024-token default.
    assert captured["max_tokens"] >= 4096


def test_read_mode_success_without_text_is_empty_output_but_write_mode_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _silent_ok(req: AgentRequest) -> AgentResult:
        return AgentResult(ok=True, text="   ", harness="claude", duration_s=0.1)

    monkeypatch.setitem(agent_cli._HARNESS_RUNNERS, "claude", _silent_ok)

    read = run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="read", harness="claude"))
    assert (read.ok, read.error_kind) == (False, "empty_output")

    write = run_agent(AgentRequest(prompt="p", cwd=tmp_path, mode="write", harness="claude"))
    assert write.ok is True


# --------------------------------------------------------------------------
# 2. No authenticated harness
# --------------------------------------------------------------------------


def test_run_read_agent_without_any_route_is_no_authenticated_harness() -> None:
    from core.line.routing import RoutingConfig, StageRoute

    cfg = RoutingConfig(stages={"grill": StageRoute(cascade=[("claude", "sonnet")], openrouter_ok=False)})
    result = stage_grill.run_read_agent("grill", "prompt", Path("."), host_caps=["git"], routing_config=cfg)
    assert result.ok is False
    assert result.error_kind == "no_authenticated_harness"


def test_retry_for_agent_failure_waits_only_for_auth_kinds() -> None:
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
    waiting = stage_grill.retry_for_agent_failure(
        AgentResult(ok=False, text="", harness="none", duration_s=0, error_kind="no_authenticated_harness"),
        "grill_agent_failed",
        now=now,
    )
    assert waiting.outcome == "retry"
    assert waiting.cause_code == "no_authenticated_harness not_before=2026-09-29T12:10:00+00:00"
    assert len(waiting.cause_code) <= CAUSE_CODE_MAX_LEN

    plain = stage_grill.retry_for_agent_failure(
        AgentResult(ok=False, text="", harness="claude", duration_s=0, error_kind="empty_output"),
        "grill_agent_failed",
        now=now,
    )
    assert plain.cause_code == "empty_output"


@pytest.mark.parametrize(
    "stage,caps,openrouter,expected",
    [
        ("grill", ["git"], False, "no_authenticated_harness"),
        ("grill", ["git"], True, None),  # read stage: OpenRouter route is fine
        ("planning", ["git"], True, None),
        ("development", ["git"], True, "no_authenticated_harness"),  # OpenRouter is read-only
        ("integration", ["git"], True, "no_authenticated_harness"),
        ("grill", ["git", "harness:codex", "harness:any"], False, None),
        ("validation", ["git"], False, None),  # deterministic stage
        ("build_deploy", [], False, None),
    ],
)
def test_agent_route_unavailable(stage: str, caps: list[str], openrouter: bool, expected: str | None) -> None:
    assert agent_route_unavailable(stage, caps, openrouter_configured=openrouter) == expected


class _CountingHandler:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.calls = 0

    def handle(self, context: StageContext) -> StageResult:
        self.calls += 1
        return StageResult(outcome="success", output_refs=[f"ref://{self.stage}"])


@pytest.fixture
def store(tmp_path: Path) -> PostgresControlStore:
    s = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    s._backend.db_path = tmp_path / "control.db"
    return s


def _accept(store: PostgresControlStore) -> str:
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test",
        external_id=f"ext-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": ["n"], "criteria": ["c"]},
    )
    return store.accept(cmd, datetime.now(UTC)).run_id


def _registry(handler_by_stage: dict[str, Any]) -> HandlerRegistry:
    return build_handlers(bindings=handler_by_stage)


def test_worker_without_harness_defers_agent_stage_instead_of_running_it(
    store: PostgresControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    handler = _CountingHandler("grill")
    run_id = _accept(store)
    worker = CloudWorker(
        worker_id="w-no-harness",
        store=store,
        registry=_registry({"grill": handler}),
        capabilities=list(DEFAULT_CAPABILITIES) + ["git"],  # no harness:* survived the probe
        autodetect_tooling=True,
    )

    assert worker.poll_and_execute_once() is True

    assert handler.calls == 0  # never ran the agent
    jobs = store.get_run_status(run_id)["jobs"]
    grill = [j for j in jobs if j["stage"] == "grill"]
    assert len(grill) >= 1
    assert all(j["status"] != "running" for j in grill)  # lease released, not stuck
    assert any((j.get("cause_code") or "").startswith("no_authenticated_harness") for j in grill)


def test_worker_with_explicit_caps_and_no_autodetect_keeps_legacy_behaviour(store: PostgresControlStore) -> None:
    handler = _CountingHandler("grill")
    _accept(store)
    worker = CloudWorker(
        worker_id="w-legacy",
        store=store,
        registry=_registry({"grill": handler}),
        capabilities=list(DEFAULT_CAPABILITIES),
    )
    assert worker.poll_and_execute_once() is True
    assert handler.calls == 1


# --------------------------------------------------------------------------
# 3. cause_code normalization + finish fallback
# --------------------------------------------------------------------------


def test_normalize_cause_code_short_passthrough_and_none() -> None:
    assert normalize_cause_code(None) is None
    assert normalize_cause_code("deploy_failed:x") == "deploy_failed:x"
    exact = "a" * CAUSE_CODE_MAX_LEN
    assert normalize_cause_code(exact) == exact


def test_normalize_cause_code_long_values_fit_and_stay_distinguishable() -> None:
    long_a = "handler_error:ValidationError: 1 validation error for AgentResult " + "x" * 400
    long_b = "handler_error:ValidationError: 1 validation error for AgentResult " + "y" * 400
    a, b = normalize_cause_code(long_a), normalize_cause_code(long_b)
    assert a is not None and b is not None
    assert len(a) <= CAUSE_CODE_MAX_LEN and len(b) <= CAUSE_CODE_MAX_LEN
    assert a != b
    assert a == normalize_cause_code(long_a)  # deterministic
    assert a.startswith("handler_error:ValidationError")

    multiline = "retry:development\n" + "log line " * 50
    assert "\n" not in (normalize_cause_code(multiline) or "")
    assert len(normalize_cause_code(multiline) or "") <= CAUSE_CODE_MAX_LEN


def test_finish_persists_normalized_cause_code_but_routing_uses_the_full_one(store: PostgresControlStore) -> None:
    class _LongFail:
        def handle(self, context: StageContext) -> StageResult:
            return StageResult(outcome="failed", cause_code="deploy_timed_out:dokploy_op_" + "z" * 200)

    run_id = _accept(store)
    worker = CloudWorker(
        worker_id="w-long-cause",
        store=store,
        registry=_registry({"grill": _LongFail()}),
        capabilities=list(DEFAULT_CAPABILITIES),
    )
    assert worker.poll_and_execute_once() is True
    grill = next(j for j in store.get_run_status(run_id)["jobs"] if j["stage"] == "grill")
    assert grill["status"] == "failed"
    assert 0 < len(grill["cause_code"]) <= CAUSE_CODE_MAX_LEN
    assert grill["cause_code"].startswith("deploy_timed_out:dokploy_op_")


def test_failed_finish_falls_back_to_short_failure_instead_of_stuck_running(
    store: PostgresControlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _accept(store)
    worker = CloudWorker(
        worker_id="w-finish-fails",
        store=store,
        registry=_registry({"grill": _CountingHandler("grill")}),
        capabilities=list(DEFAULT_CAPABILITIES),
    )
    real_finish = store.finish
    calls: list[str] = []

    def flaky_finish(claim: Any, result: StageResult, now: datetime) -> None:
        calls.append(result.outcome)
        if len(calls) == 1:
            raise RuntimeError("PostgreSQL finish failed: value too long for type character varying(64)")
        real_finish(claim, result, now)

    monkeypatch.setattr(store, "finish", flaky_finish)
    worker.poll_and_execute_once()

    assert calls == ["success", "failed"]
    grill = next(j for j in store.get_run_status(run_id)["jobs"] if j["stage"] == "grill")
    assert grill["status"] == "failed"
    assert grill["cause_code"] == "finish_failed:RuntimeError"


# --------------------------------------------------------------------------
# 4. Codex login trigger + re-probe
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _trigger(tmp_path: Path, clock: _Clock, started: list[int], *, telegram: bool = True, interval: float = 6 * 3600):
    def _spawn(target: Any) -> None:
        started.append(1)
        target()  # run inline: deterministic, no thread

    return auth_bootstrap.CodexLoginTrigger(
        stamp_path=tmp_path / "codex" / ".stamp",
        min_interval_s=interval,
        clock=clock,
        bootstrap=lambda: auth_bootstrap.HarnessProbeResult(harness="codex", ok=False, detail="pending"),
        telegram_ok=lambda: telegram,
        spawn=_spawn,
    )


def test_codex_trigger_starts_once_then_is_throttled_by_stamp(tmp_path: Path) -> None:
    clock, started = _Clock(), []
    trigger = _trigger(tmp_path, clock, started)

    assert trigger.maybe_start(["git", "harness:claude"]) is True
    assert (tmp_path / "codex" / ".stamp").is_file()
    assert trigger.maybe_start(["git"]) is False  # in-memory throttle

    # A fresh process (redeploy) sharing the same volume stamp is throttled too.
    fresh = _trigger(tmp_path, clock, started)
    clock.now += 3600
    assert fresh.maybe_start(["git"]) is False

    clock.now += 6 * 3600
    assert fresh.maybe_start(["git"]) is True
    assert len(started) == 2


def test_codex_trigger_skips_when_not_needed_or_no_telegram(tmp_path: Path) -> None:
    clock, started = _Clock(), []
    assert _trigger(tmp_path, clock, started).maybe_start(["git", "harness:codex"]) is False  # already logged in
    assert _trigger(tmp_path, clock, started).maybe_start(["git"], codex_expected=False) is False
    assert _trigger(tmp_path, clock, started, telegram=False).maybe_start(["git"]) is False
    assert started == []
    assert not (tmp_path / "codex" / ".stamp").exists()


def test_codex_trigger_never_raises_even_if_bootstrap_or_stamp_fail(tmp_path: Path) -> None:
    def _boom() -> auth_bootstrap.HarnessProbeResult:
        raise RuntimeError("codex exploded")

    blocked = tmp_path / "not_a_dir"
    blocked.write_text("file where the stamp dir should be", encoding="utf-8")
    trigger = auth_bootstrap.CodexLoginTrigger(
        stamp_path=blocked / ".stamp",
        clock=_Clock(),
        bootstrap=_boom,
        telegram_ok=lambda: True,
        spawn=lambda target: target(),
    )
    assert trigger.maybe_start(["git"]) is True  # stamp write failed (logged) but nothing raised


def test_notify_config_falls_back_to_owner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker container only has TELEGRAM_OWNER_* env; ops-only lookup dropped every message."""
    from core.integrations.telegram import TelegramConfig

    def _load(*args: Any, role: str = "ops", **kwargs: Any) -> TelegramConfig:
        if role == "owner":
            return TelegramConfig(bot_token="tok", role="owner", authorized_chat_ids=[42])
        return TelegramConfig(role=role)

    monkeypatch.setattr("core.integrations.telegram.load_telegram_config", _load)
    config = auth_bootstrap._load_notify_config()
    assert config is not None and config.authorized_chat_ids == [42]
    assert auth_bootstrap.telegram_configured() is True


def test_bootstrap_codex_login_relays_code_before_the_process_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`codex login --device-auth` keeps running until approval; the message must go out first."""
    script = tmp_path / "slow_codex.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.write('\\x1b[1mOpen https://auth.openai.com/device\\x1b[0m\\nCode: WXYZ-12345\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(1.5)\n",
        encoding="utf-8",
    )
    from tests.line.conftest import write_python_shim

    shim = write_python_shim(tmp_path / "slow_codex", script)
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: str(shim))
    monkeypatch.setattr(
        auth_bootstrap,
        "probe_codex_status",
        lambda: auth_bootstrap.HarnessProbeResult(harness="codex", ok=True, detail="ok"),
    )
    sent: list[tuple[str, float]] = []
    start = time.monotonic()
    result = auth_bootstrap.bootstrap_codex_login(
        notifier=lambda msg: sent.append((msg, time.monotonic() - start)) or True,
        timeout_s=20,
        approval_wait_s=20,
    )

    assert result.ok is True
    assert len(sent) == 1
    message, sent_at = sent[0]
    assert "https://auth.openai.com/device" in message and "\x1b" not in message
    assert "WXYZ-12345" in message
    assert sent_at < 1.4  # relayed while the CLI was still waiting for approval


def test_periodic_reprobe_readds_capability_once_login_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "harness:codex")
    clock = _Clock(0.0)
    state = {"codex_ok": False}
    hook_calls: list[list[str]] = []
    worker = CloudWorker(
        worker_id="w-reprobe",
        capability_prober=lambda harness: state["codex_ok"] if harness == "codex" else True,
        capability_probe_interval_s=600.0,
        clock=clock,
        on_capabilities_probed=lambda caps: hook_calls.append(caps),
        store=object(),  # never touched
    )
    assert "harness:codex" not in worker.capabilities  # dropped at boot: probe failed

    clock.now = 300  # interval not elapsed -> no re-probe, even after the login completed
    state["codex_ok"] = True
    assert worker.refresh_capabilities() is False
    assert "harness:codex" not in worker.capabilities

    clock.now = 601  # elapsed -> re-probed, capability re-added without a redeploy
    assert worker.refresh_capabilities() is True
    assert "harness:codex" in worker.capabilities
    assert hook_calls and "harness:codex" in hook_calls[-1]


def test_worker_compose_passes_claude_oauth_token_and_agent_cli_inherits_env() -> None:
    """The owner sets CLAUDE_CODE_OAUTH_TOKEN in Dokploy; the `claude` CLI reads it
    from the environment, so the worker must pass it and run_agent/probe_claude must
    not replace the subprocess env."""
    import inspect

    repo_root = Path(__file__).resolve().parents[1]
    compose = (repo_root / "deploy" / "dokploy" / "docker-compose.cloud.yml").read_text(encoding="utf-8")
    worker_block = compose[compose.index("darkfac-worker:") : compose.index("darkfac-backup-cron:")]
    assert "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}" in worker_block
    assert "env=" not in inspect.getsource(agent_cli._run_claude)


# --------------------------------------------------------------------------
# Two probe cadences: dropped harness every 600s, healthy harness every 3600s
# --------------------------------------------------------------------------


def _two_cadence_worker(monkeypatch: pytest.MonkeyPatch, clock: _Clock, state: dict[str, bool], calls: list[str]):
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "harness:claude,harness:codex")

    def prober(harness: str) -> bool:
        calls.append(harness)
        return state[harness]

    hooks: list[list[str]] = []
    worker = CloudWorker(
        worker_id="w-cadence",
        capability_prober=prober,
        capability_probe_interval_s=600.0,
        healthy_probe_interval_s=3600.0,
        clock=clock,
        on_capabilities_probed=lambda caps: hooks.append(caps),
        store=object(),
    )
    return worker, hooks


def test_dropped_harness_reprobed_at_600s_healthy_one_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = _Clock(0.0), []
    state = {"claude": True, "codex": False}
    worker, hooks = _two_cadence_worker(monkeypatch, clock, state, calls)
    assert sorted(calls) == ["claude", "codex"]  # boot probes everything once
    assert "harness:claude" in worker.capabilities and "harness:codex" not in worker.capabilities

    calls.clear()
    clock.now = 599
    assert worker.refresh_capabilities() is False  # tick gate: nothing at all
    assert calls == []

    clock.now = 600
    state["codex"] = True  # login completed
    assert worker.refresh_capabilities() is True
    assert calls == ["codex"]  # ONLY the dropped harness was probed; claude was not
    assert "harness:codex" in worker.capabilities and "harness:claude" in worker.capabilities
    assert hooks  # hook still fires after a probe tick


def test_healthy_harness_not_reprobed_before_3600s_and_reprobed_at_3600s(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = _Clock(0.0), []
    state = {"claude": True, "codex": True}
    worker, hooks = _two_cadence_worker(monkeypatch, clock, state, calls)
    calls.clear()

    for tick in range(600, 3600, 600):  # +600 ... +3000: every tick, nothing is due
        clock.now = float(tick)
        worker.refresh_capabilities()
    assert calls == []  # 5 ticks, zero `claude -p ok` calls for a healthy harness
    assert len(hooks) == 5  # ...but the hook still fired on each tick

    state["claude"] = False  # token expired meanwhile
    clock.now = 3600.0
    assert worker.refresh_capabilities() is True
    assert sorted(calls) == ["claude", "codex"]  # healthy ones re-probed at +3600s
    assert "harness:claude" not in worker.capabilities

    calls.clear()
    clock.now = 4200.0  # claude is now dropped -> back on the fast 600s cadence
    worker.refresh_capabilities()
    assert calls == ["claude"]


def test_force_refresh_reprobes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = _Clock(0.0), []
    worker, _ = _two_cadence_worker(monkeypatch, clock, {"claude": True, "codex": True}, calls)
    calls.clear()
    clock.now = 10.0
    worker.refresh_capabilities(force=True)
    assert sorted(calls) == ["claude", "codex"]
