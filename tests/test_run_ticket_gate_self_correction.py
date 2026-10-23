"""Delivery self-correction on gate/focal-test failure (USR-245).

USR-201 rescue: run_ticket reused a preserved worktree, the headless agent ended its turn deferring
verification ("vou esperar o pytest em segundo plano") without fixing 3 tests its own change broke, and
the official gate failed straight back to the operator with a resume command -- no automatic recovery,
and the V0 streak was reset by the manual rescue. These tests cover the fix: the delivery phase now runs
a focal-test pre-check (scoped to the files the ticket touched) before the official gate, and on either
failure reinvokes the agent in the same worktree with the distilled failure, up to
``run_ticket.GATE_FIX_ATTEMPTS`` times, before giving the ticket back.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

from core.demands.models import DeliveryStatus, UserTicket
from core.demands.store import DemandsStore
from core.git.autonomy import GitAutonomyManager, TicketCompletionReport
from core.harness.test_subagent import DistilledTestReport
from core.line.agent_cli import AgentResult, BoundedProcessResult

import run_ticket


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@darkfac.local"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True, capture_output=True)


def _seed_ticket_ledger(repo: Path, ticket: UserTicket) -> None:
    demands_dir = repo / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    DemandsStore(demands_dir / "demands.json").save_ticket(ticket)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ledger init"], cwd=str(repo), check=True, capture_output=True)


def _branch(repo: Path, name: str) -> None:
    subprocess.run(["git", "checkout", "-b", name], cwd=str(repo), check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=str(repo), check=True, capture_output=True)


def test_gate_failure_reinvokes_agent_with_distilled_log_then_retries(tmp_path: Path) -> None:
    """Gate failure: the launcher reinvokes the agent in place and reruns the gate (USR-245, AC1)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        if len(gate_calls) == 1:
            return BoundedProcessResult(stdout="1 failed, 10 passed", stderr="", returncode=1, duration_s=5.0)
        return BoundedProcessResult(stdout="11 passed", stderr="", returncode=0, duration_s=5.0)

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        assert fixed_ticket.id == "USR-245"
        assert feedback  # the distilled gate log, never empty
        (Path(cwd) / "code.py").write_text("x = 2  # fixed\n", encoding="utf-8")  # edited, NOT committed
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    fake_success_report = TicketCompletionReport(
        ok=True, ticket_id="USR-245", commit_sha="cafebabe", message="Delivered successfully",
    )

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=fake_success_report), \
         patch.object(GitAutonomyManager, "sweep_stale"):
        result = run_ticket.resume_delivery(
            "USR-245", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert result["exit_code"] == 0
    assert len(gate_calls) == 2, "the gate must be rerun after the in-place fix"
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip() == "", "the agent's uncommitted edit must have been committed before the gate reran"
    assert "fix(autocorrecao): ajuste do agente apos falha em gate [USR-245]" in subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert fix_calls == ["portao oficial (python core/harness/runner.py --quick)"]
    assert result["self_correction_log"] == [{
        "event": "reinvocation", "attempt": 1, "stage": "gate", "cause": "gate_failed",
        "duration_s": 0.1, "limit_s": run_ticket.FIX_AGENT_TIMEOUT_S, "changed": True,
    }], "the final report must list every reinvocation with cause, duration, and limit (USR-249, AC2)"


def test_gate_failure_gives_up_after_max_fix_attempts(tmp_path: Path, capsys) -> None:
    """A gate that never turns green exhausts GATE_FIX_ATTEMPTS, then fails with a resume command."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(stdout="1 failed, 10 passed", stderr="", returncode=1, duration_s=5.0)

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        (Path(cwd) / "code.py").write_text(f"x = {len(fix_calls) + 10}\n", encoding="utf-8")
        return AgentResult(ok=True, text="tentei mas nao corrigi", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        code = run_ticket.resume_delivery("USR-245", worktree_path=repo, skip_validation=False)

    assert code == 1
    assert len(gate_calls) == run_ticket.GATE_FIX_ATTEMPTS + 1, "initial attempt + GATE_FIX_ATTEMPTS retries"
    assert len(fix_calls) == run_ticket.GATE_FIX_ATTEMPTS, "no fix is attempted after the last gate run"

    captured = capsys.readouterr()
    assert "[ERRO NO PORTÃO OFICIAL]" in captured.err
    assert "Worktree preservada para diagnóstico" in captured.err
    assert "--resume-delivery USR-245" in captured.err


def test_gate_timeout_fails_immediately_without_reinvoking_the_agent(tmp_path: Path, capsys) -> None:
    """USR-249 (USR-239 incident): the official gate reprova por timeout interno do passo (3000s),
    nao por um teste de verdade -- `runner.py::run_step` retorna ao processo normalmente (0/1/2, nunca
    124), com `[STEP_FAIL] ... (timeout exceeded)` no stdout. A autocorrecao do USR-245 reinvocou o
    agente duas vezes mesmo assim (~28 min extras sem nada para o agente consertar). Agora o run termina
    na hora, com causa explicita `gate_timeout`, worktree preservada e o agente nunca e reinvocado."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(
            stdout="[STEP_FAIL] unit_and_integration_tests_parallel (timeout exceeded)\n",
            stderr="", returncode=1, duration_s=3000.1, timed_out=False,
        )

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery(
            "USR-245", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert len(gate_calls) == 1, "a timeout/infra gate failure must stop on the first attempt"
    assert fix_calls == [], "the agent must never be reinvoked after a gate timeout (USR-249)"
    assert result["ok"] is False
    assert result["cause"] == "gate_timeout"
    assert result["self_correction_log"] == [{
        "event": "timeout", "attempt": 1, "stage": "gate", "cause": "gate_timeout",
        "duration_s": 3000.1, "limit_s": run_ticket.GATE_TIMEOUT_S,
    }]

    captured = capsys.readouterr()
    assert "[TIMEOUT/INFRA NO PORTÃO OFICIAL]" in captured.err
    assert "Worktree preservada para diagnóstico" in captured.err
    assert "--resume-delivery USR-245" in captured.err
    assert "NAO reinvocado" in captured.err


def test_gate_suite_lock_contention_fails_immediately_without_reinvoking_the_agent(
    tmp_path: Path, capsys
) -> None:
    """USR-249: reproduced live while diagnosing this very ticket's own delivery -- a sibling worktree
    on the same host held `core/harness/suite_lock.py`'s machine-wide lock for the whole step budget, so
    `unit_and_integration_tests_parallel` never reached collection (`[SUITE_LOCK] waiting: held by pid
    ...]`, `discovered_count=0`, exit 124 on the *outer* harness run). Nothing in the diff caused this,
    so it must be classified the same as `(timeout exceeded)`: stop immediately, never reinvoke."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(
            stdout="[SUITE_LOCK] waiting: held by pid 261036 on AI_Notebook since 01:38:34\n"
                   "[SUITE_LOCK] timed out after 3000s waiting for the machine-wide lock\n",
            stderr="", returncode=124, duration_s=3000.0, timed_out=False,
        )

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery(
            "USR-245", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert len(gate_calls) == 1, "suite-lock contention must stop on the first attempt"
    assert fix_calls == [], "the agent must never be reinvoked after suite-lock contention (USR-249)"
    assert result["ok"] is False
    assert result["cause"] == "gate_timeout"
    assert result["self_correction_log"] == [{
        "event": "timeout", "attempt": 1, "stage": "gate", "cause": "gate_timeout",
        "duration_s": 3000.0, "limit_s": run_ticket.GATE_TIMEOUT_S,
    }]

    captured = capsys.readouterr()
    assert "[TIMEOUT/INFRA NO PORTÃO OFICIAL]" in captured.err
    assert "NAO reinvocado" in captured.err


def test_focal_timeout_fails_immediately_without_reinvoking_the_agent(tmp_path: Path, capsys) -> None:
    """USR-249: the focal pre-check (`TestSubagentEngine.execute_local`) sets `exit_code = 124` only
    from `subprocess.TimeoutExpired` -- a frozen/contended machine, not a fixable test failure. The
    official gate must never even run, and the agent must never be reinvoked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    _commit_all(repo, "feat: code whose focal pre-check times out")

    fake_timeout_report = DistilledTestReport(
        verdict="FAILED", success=False, exit_code=124, duration_seconds=600.0,
        concise_summary="Exit code 124 in 600.00s",
        agent_feedback="Tests failed with exit code 124. Summary: Exit code 124 in 600.00s",
    )

    focal_calls: list[list[str]] = []

    def fake_run_focal_tests(cwd, targets):
        focal_calls.append(targets)
        return fake_timeout_report

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(stdout="should never run", stderr="", returncode=0, duration_s=0.0)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_focal_tests", new=fake_run_focal_tests), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch("run_ticket._run_bounded", new=fake_run_bounded):
        result = run_ticket.resume_delivery(
            "USR-245", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert len(focal_calls) == 1, "a timeout focal failure must stop on the first attempt"
    assert fix_calls == [], "the agent must never be reinvoked after a focal timeout (USR-249)"
    assert gate_calls == [], "the official gate must never run after a focal timeout"
    assert result["ok"] is False
    assert result["cause"] == "focal_timeout"
    assert result["self_correction_log"] == [{
        "event": "timeout", "attempt": 1, "stage": "focal", "cause": "focal_timeout",
        "duration_s": 600.0, "limit_s": run_ticket.FOCAL_TEST_TIMEOUT_S,
    }]

    captured = capsys.readouterr()
    assert "[TIMEOUT NO PRE-TESTE FOCAL]" in captured.err
    assert "Worktree preservada para diagnóstico" in captured.err
    assert "--resume-delivery USR-245" in captured.err
    assert "NAO reinvocado" in captured.err


def test_focal_test_failure_rejects_before_the_official_gate_runs(tmp_path: Path, capsys) -> None:
    """An agent that ends its turn without verifying (USR-201) is caught by the focal pre-check,
    which never counts as completion and never even reaches the expensive official gate (AC2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from calc import add\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.factory/test_logs/\n", encoding="utf-8")
    _commit_all(repo, "feat: broken add (agent never checked)")

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        # Mirrors the USR-201 agent: ends its turn ("vou esperar o pytest em segundo plano")
        # without actually fixing anything.
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="vou esperar o pytest terminar em segundo plano", harness="claude", duration_s=0.1)

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(stdout="should never run", stderr="", returncode=0, duration_s=0.0)

    focal_ram_ready = run_ticket.gate_ram.RamWaitResult(
        True, run_ticket.gate_ram.RamReading(available_gib=8.0, workers=2), waited_s=0.0
    )
    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(run_ticket.gate_ram, "remote_workers_configured", return_value=False), \
         patch.object(run_ticket.gate_ram, "wait_for_gate_ram", return_value=focal_ram_ready), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch("run_ticket._run_bounded", new=fake_run_bounded):
        code = run_ticket.resume_delivery("USR-245", worktree_path=repo, skip_validation=False)

    assert code != 0
    assert len(fix_calls) == 1, "an agent that changed nothing is not reinvoked again (USR-249)"
    assert gate_calls == [], "the official gate must never run while focal tests keep rejecting the ticket"

    captured = capsys.readouterr()
    assert "[ERRO NOS TESTES FOCAIS]" in captured.err
    assert "Worktree preservada para diagnóstico" in captured.err
    assert "--resume-delivery USR-245" in captured.err


def test_focal_sweep_is_dropped_when_a_changed_module_is_too_widely_imported(tmp_path: Path) -> None:
    """USR-188: a foundational module (e.g. `routing.py`) is genuinely imported by dozens of test
    files; sweeping all of them in made the pre-check run well past FOCAL_TEST_TIMEOUT_S on a
    memory-constrained worker (xdist capped to 1 worker). Past FOCAL_SWEEP_MAX_EXTRA_FILES matches, the
    sweep is dropped entirely rather than trimmed arbitrarily, keeping only the changed test files
    themselves -- the official gate downstream remains the complete, final check either way."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    changed_test = tests_dir / "test_widely_used.py"
    changed_test.write_text("def test_noop():\n    pass\n", encoding="utf-8")
    for i in range(run_ticket.FOCAL_SWEEP_MAX_EXTRA_FILES + 1):
        (tests_dir / f"test_dependent_{i}.py").write_text(
            "from core.line import widely_used\n", encoding="utf-8"
        )

    targets = run_ticket._focal_test_targets(
        tmp_path, ["core/line/widely_used.py", "tests/test_widely_used.py"]
    )

    assert targets == ["tests/test_widely_used.py"]


def test_focal_sweep_is_kept_under_the_cap(tmp_path: Path) -> None:
    """The counterpart of the previous test: a module referenced by a small, legitimate number of
    tests still gets its dependents swept in."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(run_ticket.FOCAL_SWEEP_MAX_EXTRA_FILES):
        (tests_dir / f"test_dependent_{i}.py").write_text(
            "from core.line import narrow_module\n", encoding="utf-8"
        )

    targets = run_ticket._focal_test_targets(tmp_path, ["core/line/narrow_module.py"])

    assert len(targets) == run_ticket.FOCAL_SWEEP_MAX_EXTRA_FILES


def test_focal_test_timeout_scales_with_swept_target_count() -> None:
    """USR-249: a flat 600s (`FOCAL_TEST_TIMEOUT_S`) was calibrated against the changed-files-only
    set (~106s observed), never against a full sweep of up to `FOCAL_SWEEP_MAX_EXTRA_FILES` extra
    files. A legitimate 16-file sweep (right at the cap) exceeded 600s serialized on a 1-xdist-worker
    host even though every individual test passed, so the budget must grow with how many files are
    actually swept -- bounded so a maximal sweep still stays far cheaper than `GATE_TIMEOUT_S`."""
    # A small target set keeps the original flat floor.
    assert run_ticket._focal_test_timeout_s(1) == run_ticket.FOCAL_TEST_TIMEOUT_S
    assert run_ticket._focal_test_timeout_s(0) == run_ticket.FOCAL_TEST_TIMEOUT_S

    # Past the point where target_count * per-target budget exceeds the floor, the budget scales up.
    small_count = int(run_ticket.FOCAL_TEST_TIMEOUT_S // run_ticket.FOCAL_TEST_TIMEOUT_PER_TARGET_S)
    scaled = run_ticket._focal_test_timeout_s(small_count + 5)
    assert scaled > run_ticket.FOCAL_TEST_TIMEOUT_S
    assert scaled == (small_count + 5) * run_ticket.FOCAL_TEST_TIMEOUT_PER_TARGET_S

    # A maximal sweep (FOCAL_SWEEP_MAX_EXTRA_FILES changed/swept test files) never exceeds the ceiling,
    # which stays well under GATE_TIMEOUT_S so the pre-check still fails fast relative to the full gate.
    max_sweep = run_ticket._focal_test_timeout_s(run_ticket.FOCAL_SWEEP_MAX_EXTRA_FILES + 1)
    assert max_sweep <= run_ticket.FOCAL_TEST_TIMEOUT_CEILING_S
    assert run_ticket.FOCAL_TEST_TIMEOUT_CEILING_S < run_ticket.GATE_TIMEOUT_S

    # A pathologically large count never exceeds the ceiling either.
    assert run_ticket._focal_test_timeout_s(10_000) == run_ticket.FOCAL_TEST_TIMEOUT_CEILING_S


def test_run_focal_tests_passes_the_scaled_timeout_to_the_engine(tmp_path: Path) -> None:
    """`_run_focal_tests` must hand `TestSubagentEngine` the scaled budget (`_focal_test_timeout_s`),
    not the flat `FOCAL_TEST_TIMEOUT_S`, once the target list exceeds the small-set floor (USR-249)."""
    from core.harness.test_subagent import TestSubagentEngine

    targets = [f"tests/test_dependent_{i}.py" for i in range(run_ticket.FOCAL_SWEEP_MAX_EXTRA_FILES)]
    captured_instructions = []

    def fake_execute_local(self, instruction):
        captured_instructions.append(instruction)
        return None

    ready = run_ticket.gate_ram.RamWaitResult(
        True, run_ticket.gate_ram.RamReading(available_gib=8.0, workers=2), waited_s=0.0
    )
    with patch.object(run_ticket.gate_ram, "remote_workers_configured", return_value=False), \
         patch.object(run_ticket.gate_ram, "remote_worker_available", return_value=False), \
         patch.object(run_ticket.gate_ram, "wait_for_gate_ram", return_value=ready) as wait_for_ram, \
         patch.object(TestSubagentEngine, "execute_local", new=fake_execute_local):
        run_ticket._run_focal_tests(tmp_path, targets)

    wait_for_ram.assert_called_once()
    assert len(captured_instructions) == 1
    expected = run_ticket._focal_test_timeout_s(len(targets))
    assert captured_instructions[0].timeout_seconds == int(expected)
    assert captured_instructions[0].worker_mode == "local"
    assert expected > run_ticket.FOCAL_TEST_TIMEOUT_S, "the test setup should exercise actual scaling"


def test_run_focal_tests_skips_local_pretest_when_remote_workers_are_configured(tmp_path: Path, caplog) -> None:
    """USR-265: let the official dispatcher own health checks, queueing, and fallback."""
    from core.harness.test_subagent import TestSubagentEngine

    caplog.set_level(logging.INFO, logger="darkfac.run_ticket")
    with patch.object(run_ticket.gate_ram, "remote_workers_configured", return_value=True), \
         patch.object(run_ticket.gate_ram, "remote_worker_available", return_value=False), \
         patch.object(run_ticket.gate_ram, "wait_for_gate_ram") as wait_for_ram, \
         patch.object(TestSubagentEngine, "execute_local") as execute_local:
        report = run_ticket._run_focal_tests(tmp_path, ["tests/test_git_autonomy_ci_gate.py"])

    assert report is None
    wait_for_ram.assert_not_called()
    execute_local.assert_not_called()
    assert "workers remotos configurados" in caplog.text
    assert "validacao integral ficam para o portao oficial" in caplog.text


def test_remote_workers_configured_ignores_transient_health_and_busy_state() -> None:
    """USR-265: configuration, not a single health probe, decides whether to avoid local focal work."""
    from core.harness import remote_dispatch

    with patch.object(remote_dispatch, "_effective_mode", return_value="auto"), \
         patch.object(remote_dispatch, "_unconditional_disable_reason", return_value=None), \
         patch.object(remote_dispatch, "worker_urls", return_value=["http://worker.test"]), \
         patch.object(remote_dispatch, "probe_health", return_value=None):
        assert run_ticket.gate_ram.remote_workers_configured() is True

    with patch.object(remote_dispatch, "_effective_mode", return_value="off"), \
         patch.object(remote_dispatch, "_unconditional_disable_reason", return_value=None), \
         patch.object(remote_dispatch, "worker_urls", return_value=["http://worker.test"]):
        assert run_ticket.gate_ram.remote_workers_configured() is False


def test_run_focal_tests_skips_local_pretest_when_ram_wait_expires(tmp_path: Path, caplog) -> None:
    """USR-265: do not start a heavy focal suite after the USR-262 RAM wait fails."""
    from core.harness.test_subagent import TestSubagentEngine

    insufficient = run_ticket.gate_ram.RamWaitResult(
        False, run_ticket.gate_ram.RamReading(available_gib=1.0, workers=0), waited_s=120.0
    )
    with patch.object(run_ticket.gate_ram, "remote_workers_configured", return_value=False), \
         patch.object(run_ticket.gate_ram, "remote_worker_available", return_value=False), \
         patch.object(run_ticket.gate_ram, "wait_for_gate_ram", return_value=insufficient) as wait_for_ram, \
         patch.object(TestSubagentEngine, "execute_local") as execute_local:
        report = run_ticket._run_focal_tests(tmp_path, ["tests/test_git_autonomy_ci_gate.py"])

    assert report is None
    wait_for_ram.assert_called_once()
    execute_local.assert_not_called()
    assert "RAM insuficiente" in caplog.text


def test_focal_test_fix_on_second_pass_reaches_merge_without_resume_delivery(tmp_path: Path, capsys) -> None:
    """A fake agent that leaves a broken test on the first pass and fixes it on the second: the ticket
    reaches merge from a single `resume_delivery` call, with no `--resume-delivery` round trip (AC3)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-245", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)

    _branch(repo, "ticket/usr-245")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from calc import add\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _commit_all(repo, "feat: broken add on first pass")

    fix_calls: list[str] = []

    def fake_fix_that_actually_fixes_it(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        # The fixed source must differ in SIZE from the broken one ("a - b"): a .pyc is validated by the source
        # mtime in whole seconds plus its size, and the focal pytest re-run happens within the same second on a
        # fast runner, so a same-size rewrite kept the stale bytecode (CI flake that turned main red on Linux).
        (Path(cwd) / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n", encoding="utf-8")
        return AgentResult(ok=True, text="corrigido", harness="claude", duration_s=0.1)

    fake_success_report = TicketCompletionReport(
        ok=True, ticket_id="USR-245", commit_sha="deadbeef", message="Delivered successfully",
    )

    focal_ram_ready = run_ticket.gate_ram.RamWaitResult(
        True, run_ticket.gate_ram.RamReading(available_gib=8.0, workers=2), waited_s=0.0
    )
    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(run_ticket.gate_ram, "remote_workers_configured", return_value=False), \
         patch.object(run_ticket.gate_ram, "wait_for_gate_ram", return_value=focal_ram_ready), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix_that_actually_fixes_it), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=fake_success_report), \
         patch.object(GitAutonomyManager, "sweep_stale"):
        code = run_ticket.resume_delivery("USR-245", worktree_path=repo, skip_validation=True)

    assert code == 0
    assert fix_calls == ["testes focais dos arquivos alterados"], "exactly one correction pass was needed"

    captured = capsys.readouterr()
    assert "[SUCESSO] Ticket USR-245 entregue e validado com sucesso!" in captured.out


def _gate_first_output_reinvokes(tmp_path: Path, stdout: str, returncode: int = 1) -> tuple[list[str], dict]:
    """Run resume_delivery with a fake gate whose first run prints `stdout`; the second passes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-249", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)
    _branch(repo, "ticket/usr-249")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        if len(gate_calls) == 1:
            return BoundedProcessResult(stdout=stdout, stderr="", returncode=returncode, duration_s=5.0)
        return BoundedProcessResult(stdout="11 passed", stderr="", returncode=0, duration_s=5.0)

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        (Path(cwd) / "code.py").write_text("x = 3  # fixed\n", encoding="utf-8")
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    report = TicketCompletionReport(ok=True, ticket_id="USR-249", commit_sha="cafebabe", message="ok")
    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=report), \
         patch.object(GitAutonomyManager, "sweep_stale"):
        result = run_ticket.resume_delivery(
            "USR-249", worktree_path=repo, skip_validation=False, return_result=True
        )
    return fix_calls, result


def test_suite_lock_acquired_then_real_pytest_failure_reinvokes_the_agent(tmp_path: Path) -> None:
    out = "[SUITE_LOCK] acquired slot 1 after waiting 42s\nFAILED tests/x.py::t - assert 0\n1 failed, 3 passed\n"
    fix_calls, result = _gate_first_output_reinvokes(tmp_path, out)
    assert len(fix_calls) == 1
    assert result["exit_code"] == 0


def test_remote_fallback_with_real_pytest_failure_reinvokes_the_agent(tmp_path: Path) -> None:
    out = "[REMOTE_JOB_LOST] worker gone\n[REMOTE_FALLBACK] running locally\nFAILED tests/x.py::t - assert 0\n"
    fix_calls, result = _gate_first_output_reinvokes(tmp_path, out)
    assert len(fix_calls) == 1
    assert result["exit_code"] == 0


def test_suite_lock_timed_out_does_not_reinvoke_the_agent(tmp_path: Path) -> None:
    fix_calls, result = _gate_first_output_reinvokes(
        tmp_path, "[SUITE_LOCK] timed out after 3000s waiting for the lock\n"
    )
    assert fix_calls == []
    assert result["cause"] == "gate_timeout"


def test_remote_job_lost_without_pytest_failure_does_not_reinvoke_the_agent(tmp_path: Path) -> None:
    fix_calls, result = _gate_first_output_reinvokes(tmp_path, "[REMOTE_JOB_LOST] worker gone\n")
    assert fix_calls == []
    assert result["cause"] == "gate_timeout"


def _repo_on_ticket_branch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-249", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)
    _branch(repo, "ticket/usr-249")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")
    return repo


def test_fix_agent_that_edits_without_committing_is_committed_and_gate_reruns_clean(tmp_path: Path) -> None:
    """USR-249: the gate refuses a dirty candidate, so the loop commits the agent's edit before rerunning."""
    repo = _repo_on_ticket_branch(tmp_path)
    dirty_at_gate: list[bool] = []

    def fake_run_bounded(argv, **kwargs):
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip())
        dirty_at_gate.append(dirty)
        if dirty:
            return BoundedProcessResult(
                stdout="[ERROR] Candidate worktree is dirty; commit or use a clean checkout\n[HARNESS_FAIL]",
                stderr="", returncode=1, duration_s=0.1,
            )
        if len(dirty_at_gate) == 1:
            return BoundedProcessResult(stdout="1 failed", stderr="", returncode=1, duration_s=0.1)
        return BoundedProcessResult(stdout="11 passed", stderr="", returncode=0, duration_s=0.1)

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        (Path(cwd) / "code.py").write_text("x = 99  # fixed\n", encoding="utf-8")
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    report = TicketCompletionReport(ok=True, ticket_id="USR-249", commit_sha="cafebabe", message="ok")
    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch.object(GitAutonomyManager, "complete_ticket", return_value=report), \
         patch.object(GitAutonomyManager, "sweep_stale"):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    assert result["exit_code"] == 0
    assert dirty_at_gate == [False, False]
    assert result["self_correction_log"][0]["changed"] is True


def test_fix_agent_that_changes_nothing_stops_with_fix_agent_no_changes(tmp_path: Path) -> None:
    repo = _repo_on_ticket_branch(tmp_path)
    gate_calls: list[list[str]] = []
    fix_calls: list[str] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(argv)
        return BoundedProcessResult(stdout="1 failed, 10 passed", stderr="", returncode=1, duration_s=0.1)

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="nada a fazer", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    assert result["ok"] is False
    assert result["cause"] == "fix_agent_no_changes"
    assert len(fix_calls) == 1 and len(gate_calls) == 1
    assert result["self_correction_log"][0]["changed"] is False


def test_failed_gate_test_ids_reach_delivery_child_console_report(tmp_path: Path, capsys) -> None:
    repo = _repo_on_ticket_branch(tmp_path)
    gate_output = "\n".join([
        "=========================== short test summary info ===========================",
        "FAILED tests/test_alpha.py::test_first - AssertionError",
        "FAILED tests/test_beta.py::test_second - RuntimeError",
        "========================= 2 failed in 0.1s ==========================",
    ])

    def fake_run_bounded(argv, **kwargs):
        return BoundedProcessResult(stdout=gate_output, stderr="", returncode=1, duration_s=0.1)

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        return AgentResult(ok=True, text="nada a fazer", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    run_ticket._report_delivery_child_failure(
        BoundedProcessResult(returncode=1), result, True, "USR-249", repo
    )
    console = capsys.readouterr().err
    assert result["failed_tests"] == [
        "tests/test_alpha.py::test_first", "tests/test_beta.py::test_second",
    ]
    assert "Testes reprovados: tests/test_alpha.py::test_first, tests/test_beta.py::test_second" in console
    assert "Cauda do log do portao:" in console
    assert "2 failed in 0.1s" in console


def test_failed_gate_without_pytest_failure_lines_shows_redacted_log_tail(tmp_path: Path, capsys) -> None:
    repo = _repo_on_ticket_branch(tmp_path)
    secret = "sk-abcdefghijklmnopqrs"
    gate_output = (
        "runner infrastructure failed\n[HARNESS_FAIL] worker unavailable\n"
        f"OPENAI_API_KEY={secret}"
    )

    def fake_run_bounded(argv, **kwargs):
        return BoundedProcessResult(stdout=gate_output, stderr="", returncode=1, duration_s=0.1)

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        return AgentResult(ok=True, text="nada a fazer", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    run_ticket._report_delivery_child_failure(
        BoundedProcessResult(returncode=1), result, True, "USR-249", repo
    )
    console = capsys.readouterr().err
    assert result["failed_tests"] == []
    assert "Cauda do log do portao:" in console
    assert "runner infrastructure failed\n[HARNESS_FAIL] worker unavailable" in console
    assert secret not in result["gate_log_tail"]
    assert result["gate_log_tail"].endswith("[REDACTED]")


def test_gate_reporting_dirty_worktree_stops_without_reinvoking_the_agent(tmp_path: Path) -> None:
    repo = _repo_on_ticket_branch(tmp_path)
    fix_calls: list[str] = []

    def fake_run_bounded(argv, **kwargs):
        return BoundedProcessResult(
            stdout="[ERROR] Candidate worktree is dirty; commit or use a clean checkout before running "
                   "the official harness\n[HARNESS_FAIL]",
            stderr="", returncode=1, duration_s=0.1,
        )

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    assert fix_calls == []
    assert result["ok"] is False
    assert result["cause"] == "gate_dirty_worktree"


def test_fix_commit_failure_ends_the_loop_with_fix_commit_failed(tmp_path: Path, capsys) -> None:
    repo = _repo_on_ticket_branch(tmp_path)
    fix_calls: list[str] = []

    def fake_run_bounded(argv, **kwargs):
        return BoundedProcessResult(stdout="1 failed, 10 passed", stderr="", returncode=1, duration_s=0.1)

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        (Path(cwd) / "code.py").write_text("x = 7\n", encoding="utf-8")
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix), \
         patch.object(GitAutonomyManager, "commit_ticket", side_effect=RuntimeError("secret detected")):
        result = run_ticket.resume_delivery("USR-249", worktree_path=repo, skip_validation=False, return_result=True)

    assert result["ok"] is False
    assert result["cause"] == "fix_commit_failed"
    assert len(fix_calls) == 1
    assert result["self_correction_log"][-1] == {
        "event": "infra_error", "attempt": 1, "stage": "gate", "cause": "fix_commit_failed",
        "error": "secret detected",
    }
    err = capsys.readouterr().err
    assert "Worktree preservada" in err
    assert "secret detected" in err


def test_gate_requires_remote_when_configured_and_local_ram_cannot_fit_two_workers() -> None:
    """USR-354: busy_timeout on a remote slot must requeue, not fall to a 1-worker local gate."""
    ga = run_ticket.gate_ram
    low = ga.RamReading(4.0, 1)
    ok = ga.RamReading(16.0, 3)
    with patch.object(ga, "remote_workers_configured", return_value=True), \
         patch.object(ga, "measure_workers", return_value=low):
        assert run_ticket._gate_should_require_remote() is True
        assert run_ticket._wait_gate_ram_if_local(True) is None
    with patch.object(ga, "remote_workers_configured", return_value=True), \
         patch.object(ga, "measure_workers", return_value=ok):
        assert run_ticket._gate_should_require_remote() is False
    with patch.object(ga, "remote_workers_configured", return_value=False), \
         patch.object(ga, "measure_workers", return_value=low):
        assert run_ticket._gate_should_require_remote() is False


def test_gate_remote_busy_timeout_requeues_without_agent_or_local_fallback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """USR-354: busy_timeout on the remote slot (RAM local < 2 workers) -> gate_remote_unavailable."""
    # Fleet mode forces --local; CI runners may have it on, so pin the non-fleet path.
    from core.line import line_lock

    monkeypatch.delenv("DARKFAC_FLEET_MODE", raising=False)
    monkeypatch.setattr(line_lock, "is_fleet_mode", lambda *a, **k: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    ticket = UserTicket(
        id="USR-354", project_id="darkfac", title="Test Ticket",
        problem_statement="Test problem", status=DeliveryStatus.PLANNED,
    )
    _seed_ticket_ledger(repo, ticket)
    _branch(repo, "ticket/usr-354")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat: code")

    gate_calls: list[list[str]] = []

    def fake_run_bounded(argv, **kwargs):
        gate_calls.append(list(argv))
        return BoundedProcessResult(
            stdout="", stderr="RemoteRequiredError: --remote-required: all workers busy among ['http://desktop']\n",
            returncode=1, duration_s=52.0, timed_out=False,
        )

    fix_calls: list[str] = []

    def fake_fix(cwd, fixed_ticket, stage_label, feedback):
        fix_calls.append(stage_label)
        return AgentResult(ok=True, text="fixed", harness="claude", duration_s=0.1)

    ga = run_ticket.gate_ram
    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch.object(ga, "remote_workers_configured", return_value=True), \
         patch.object(ga, "measure_workers", return_value=ga.RamReading(4.0, 1)), \
         patch("run_ticket._run_bounded", new=fake_run_bounded), \
         patch("run_ticket._invoke_fix_agent", new=fake_fix):
        result = run_ticket.resume_delivery(
            "USR-354", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert len(gate_calls) == 1
    assert "--remote-required" in gate_calls[0] and "--local" not in gate_calls[0]
    assert fix_calls == []
    assert result["ok"] is False
    assert result["cause"] == run_ticket.GATE_CAUSE_REMOTE_UNAVAILABLE
    assert "--resume-delivery USR-354" in capsys.readouterr().err
