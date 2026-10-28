"""Shared deterministic fixtures and safety rails for the official suite."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

# These settings direct the harness at real remote workers or change how long it
# waits for them. Scrub them before test modules are imported or collected: an
# autouse fixture alone runs too late for module-level configuration and is not
# inherited safely by pytest subprocesses started during collection.
AMBIENT_REMOTE_INFRA_ENV = (
    "DARKFAC_TEST_WORKERS",
    "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC",
    "DARKFAC_REMOTE_HARNESS",
    "DARKFAC_WORKER_TOKEN",
    "DARKFAC_HARNESS_WORKER_JOB",
    "DARKFAC_WORKER_DEAD_PROBES",
    "DARKFAC_REMOTE_BUSY_WAIT_SEC",
    "DARKFAC_HARNESS_REMOTE_WAIT_SEC",
    "DARKFAC_WORKER_SILENCE_FALLBACK_SEC",
    "DARKFAC_REMOTE_DISPATCH_TEST_OVERRIDE",
    "REMOTE_HARNESS_URL",
    "REMOTE_HARNESS_URLS",
)
for _remote_env_name in AMBIENT_REMOTE_INFRA_ENV:
    os.environ.pop(_remote_env_name, None)

# These values describe an active delivery run, not a test-suite default. In
# particular, inheriting the lock/cancel/run markers can make run_ticket skip
# acquiring its own lock or attach publications to the parent run. Scrub before
# importing test modules, just like remote worker configuration above.
for _test_control_env_name in (
    "DARKFAC_LINE_LOCK_HELD",
    "DARKFAC_LOCAL_CANCEL_PARENT",
    "DARKFAC_LOCAL_RUN_ID",
    "DARKFAC_RUN_ID",
):
    os.environ.pop(_test_control_env_name, None)

# Always replace the inherited root before test modules import code that may
# resolve paths at import time. Session and per-test fixtures narrow this root
# further where state can be mutated concurrently.
_EARLY_STATE_ROOT = tempfile.mkdtemp(prefix="darkfac_state_")
os.environ["DARKFAC_STATE_ROOT"] = _EARLY_STATE_ROOT

# Always isolated, even when inherited: a test that builds the worker app without
# a state dir (``create_worker_app()`` -> ``JobManager``) recovers the persisted
# jobs it finds there. Pointed at the host's real worker state, it "recovered"
# the live job running this very suite (its owner's psutil start time drifts
# after a VM freeze/restore, so the live worker looked dead) and SIGTERMed the
# job's process tree ~2 s into the parallel step (exit -15 on the grokbox).
os.environ["DARKFAC_WORKER_STATE_DIR"] = tempfile.mkdtemp(prefix="darkfac_worker_state_")

import numpy as np
import pytest
import soundfile as sf

IMPORT_ROOT = Path(__file__).resolve().parent.parent
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.harness import suite_lock as _suite_lock
from tests import _cache_dir, _tree_hygiene, _worker_capacity


def _git_for_fixture(repo: Path, *args: str) -> str:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture(scope="session")
def git_repo_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Immutable seed repositories copied by git-heavy tests instead of recreated per test."""
    root = tmp_path_factory.mktemp("git_repo_templates")
    populated = root / "populated"
    populated.mkdir()
    _git_for_fixture(populated, "init", "-q", "-b", "main")
    _git_for_fixture(populated, "config", "user.email", "tests@example.com")
    _git_for_fixture(populated, "config", "user.name", "DarkFac Tests")
    _git_for_fixture(populated, "config", "commit.gpgsign", "false")
    _git_for_fixture(populated, "config", "core.autocrlf", "false")
    (populated / "a.txt").write_text("one\n", encoding="utf-8")
    _git_for_fixture(populated, "add", "-A")
    _git_for_fixture(populated, "commit", "-q", "-m", "base")
    (populated / "a.txt").write_text("two\n", encoding="utf-8")
    _git_for_fixture(populated, "add", "-A")
    _git_for_fixture(populated, "commit", "-q", "-m", "second")

    empty = root / "empty"
    empty.mkdir()
    _git_for_fixture(empty, "init", "-q", "-b", "main")
    _git_for_fixture(empty, "config", "user.email", "tests@example.com")
    _git_for_fixture(empty, "config", "user.name", "DarkFac Tests")
    _git_for_fixture(empty, "config", "commit.gpgsign", "false")
    _git_for_fixture(empty, "config", "core.autocrlf", "false")
    return {"populated": populated, "empty": empty}


@pytest.fixture
def copy_git_repo(git_repo_templates: dict[str, Path]):
    """Copy a session template into a test-owned path without invoking git."""
    def _copy(name: str, destination: Path) -> Path:
        shutil.copytree(git_repo_templates[name], destination)
        return destination
    return _copy

# Mitigate Windows WMI query crashes (0x8007000e) during worker startup (USR-95)
_worker_capacity.mitigate_windows_wmi_startup()

# Set by pytest_sessionstart (xdist controller) or pytest_collection_finish
# (plain, non-distributed run). Released in pytest_sessionfinish. A raw
# complete-suite `pytest` invocation by ANY
# harness/agent on this machine goes through this lock too -- not only the
# official `core/harness/runner.py` -- so two full suites never fight over
# CPU and sqlite state at once.
_session_suite_lock: _suite_lock.SuiteLock | None = None

# Dirty-set fingerprint of the checkout taken by the session controller at
# sessionstart; compared at sessionfinish (see tests/_tree_hygiene.py). None
# means the guard is off for this process (disabled, nested run, xdist worker
# or no usable git).
_tree_fingerprint_before: _tree_hygiene.Fingerprint | None = None
_tree_guard_owns_env = False


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def _lock_should_be_skipped(config: pytest.Config) -> bool:
    return (
        _suite_lock.is_disabled()
        or _suite_lock.is_held_by_ancestor()
        or _is_xdist_worker(config)
    )


def _requested_numprocesses(config: pytest.Config) -> int | None:
    try:
        value = config.getoption("numprocesses")
    except (ValueError, AttributeError):
        return None
    if not value or value in ("0",):
        return None
    return value


def _is_explicit_focal_selection(config: pytest.Config) -> bool:
    """Whether pytest was explicitly asked to select less than the test suite.

    Selection happens before collection finishes, but the resulting item count
    is not enough to identify a focal run: one file can contain 100+ tests.
    Keep default ``testpaths`` (the complete suite) eligible for the lock while
    treating explicit paths, node IDs, and pytest filters as focal selections.
    """

    option = config.option
    if any(
        getattr(option, name, None)
        for name in ("keyword", "markexpr", "deselect", "ignore", "ignore_glob")
    ):
        return True

    selected_paths = getattr(config, "args", ())
    if not selected_paths:
        return False

    rootpath = Path(getattr(config, "rootpath", IMPORT_ROOT))

    def resolve_from_root(path: Path) -> Path:
        return (path if path.is_absolute() else rootpath / path).resolve()

    default_paths = config.getini("testpaths")
    default_resolved = {resolve_from_root(Path(path)) for path in default_paths}
    for selected in selected_paths:
        # Pytest node IDs append ``::class::test`` to a file path.
        selected_path = str(selected).split("::", 1)[0]
        if resolve_from_root(Path(selected_path)) not in default_resolved:
            return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Enforce memory-safe worker limit on xdist before workers spawn (USR-95/USR-143)."""
    safe_cap = _worker_capacity.calculate_safe_worker_cap()
    current_max = getattr(config.option, "maxprocesses", None)
    if current_max is not None and current_max > 0:
        config.option.maxprocesses = min(current_max, safe_cap)
    else:
        config.option.maxprocesses = safe_cap

    if not _is_xdist_worker(config):
        print(f"[WORKERS] allowed {_worker_capacity.explain_worker_budget()}")


@pytest.hookimpl(tryfirst=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Provide memory-safe number of workers when --numprocesses=auto is requested (USR-95)."""
    return _worker_capacity.calculate_safe_worker_cap()


def _acquire_session_lock() -> None:
    global _session_suite_lock
    if _session_suite_lock is not None:
        return
    lock = _suite_lock.SuiteLock()
    lock.acquire()
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    _session_suite_lock = lock


def _start_tree_guard(config: pytest.Config) -> None:
    """Fingerprint the checkout so sessionfinish can name whatever the suite dirtied."""

    global _tree_fingerprint_before, _tree_guard_owns_env
    if (
        _is_xdist_worker(config)
        or _tree_hygiene.is_disabled()
        or _tree_hygiene.is_nested_session()
    ):
        return
    _tree_fingerprint_before = _tree_hygiene.take_fingerprint(IMPORT_ROOT)
    if _tree_fingerprint_before is not None:
        os.environ[_tree_hygiene.ENV_ACTIVE] = "1"
        _tree_guard_owns_env = True


def _finish_tree_guard(session: pytest.Session) -> None:
    """Fail the session (and print each path) when the suite altered the checkout."""

    global _tree_fingerprint_before, _tree_guard_owns_env
    before, _tree_fingerprint_before = _tree_fingerprint_before, None
    if _tree_guard_owns_env:
        os.environ.pop(_tree_hygiene.ENV_ACTIVE, None)
        _tree_guard_owns_env = False
    if before is None:
        return
    after = _tree_hygiene.take_fingerprint(IMPORT_ROOT)
    if after is None:
        return
    violations = _tree_hygiene.diff_fingerprints(before, after)
    if not violations:
        return
    lines = _tree_hygiene.format_report(violations)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print("\n".join(lines), file=sys.stderr)
    else:
        reporter.write_line("")
        reporter.write_sep("=", "tree hygiene", red=True, bold=True)
        for line in lines:
            reporter.write_line(line)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = int(pytest.ExitCode.TESTS_FAILED)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Keep ``.pytest_cache`` (protected ACL on Windows) out of the worktree."""

    _cache_dir.redirect_cache_dir(config)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Lock complete xdist runs before workers inherit the held-lock marker."""

    config = session.config
    if (
        not _lock_should_be_skipped(config)
        and _requested_numprocesses(config) is not None
        and not _is_explicit_focal_selection(config)
    ):
        _acquire_session_lock()
    _start_tree_guard(config)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Lock complete-suite runs after collection, leaving focal runs unqueued."""

    config = session.config
    if _lock_should_be_skipped(config):
        return
    if _session_suite_lock is not None:
        return
    if _is_explicit_focal_selection(config):
        return
    _acquire_session_lock()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # trylast: on the xdist controller this runs after DSession has shut the
    # workers down, so none is still writing while we re-fingerprint.
    global _session_suite_lock
    try:
        _finish_tree_guard(session)
    finally:
        if _session_suite_lock is not None:
            _session_suite_lock.release()
            _session_suite_lock = None

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    path = Path(tempfile.gettempdir()) / f"darkfac_active_{worker}.txt"
    try:
        path.write_text(item.nodeid, encoding="utf-8")
    except Exception:
        pass


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    path = Path(tempfile.gettempdir()) / f"darkfac_active_{worker}.txt"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


_SECRET_ENVIRONMENT_KEYS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "XAI_API_KEY",
    "XAI_MANAGEMENT_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "SILICONFLOW_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "MINIMAX_API_KEY",
    "ARTIFICIAL_ANALYSIS_API_KEY",
    # OA-010 configures this on the Hub host so /api/telemetry/events and /api/usage/sync require
    # X-DarkFac-Telemetry-Key. Left set, a test that posts without the header gets a real 401 instead
    # of exercising the "no key configured" path. A test that needs the authenticated path sets it
    # itself via monkeypatch (USR-229).
    "DARKFAC_TELEMETRY_KEY",
    # Production control-store URLs: a developer machine that exports one must never let a test accept
    # demands into (or claim jobs from) the real line. On 2026-10-07 `tests/test_cloud_e2e_task.py` did
    # exactly that and left three fake-grilled "HF-03-08" runs looping on `grill_not_ready` in
    # production. A test that needs a URL sets it itself (monkeypatch/mock://), as before.
    "DARKFAC_HF02_DATABASE_URL",
    "DARKHUB_LINE_DATABASE_URL",
    "DARKHUB_CONTROL_DATABASE_URL",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("darkfac-audio")
    group.addoption(
        "--run-live-audio",
        action="store_true",
        help="Enable tests marked live (manual audio/provider experiment).",
    )
    group.addoption(
        "--run-gpu-audio",
        action="store_true",
        help="Enable tests marked gpu (manual CUDA experiment).",
    )
    group.addoption(
        "--allow-network",
        action="store_true",
        help="Allow network access for explicitly opted-in live experiments.",
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    run_live = config.getoption("--run-live-audio") or _env_flag(
        "DARKFAC_RUN_LIVE_AUDIO"
    )
    run_gpu = config.getoption("--run-gpu-audio") or _env_flag(
        "DARKFAC_RUN_GPU_AUDIO"
    )
    skip_live = pytest.mark.skip(
        reason="ensaio live desabilitado; use --run-live-audio explicitamente"
    )
    skip_gpu = pytest.mark.skip(
        reason="ensaio GPU desabilitado; use --run-gpu-audio explicitamente"
    )

    for item in items:
        if "live" in item.keywords and not run_live:
            item.add_marker(skip_live)
        if "gpu" in item.keywords and not run_gpu:
            item.add_marker(skip_gpu)


@pytest.fixture(scope="session", autouse=True)
def offline_test_environment(request: pytest.FixtureRequest) -> Iterator[None]:
    """Remove credentials and block network for every default test run."""

    previous_values = {key: os.environ.get(key) for key in _SECRET_ENVIRONMENT_KEYS}
    offline_values = {
        "DARKFAC_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        # Skip the real GPU/torch probe in core/telemetry/hardware.py: it
        # can cost several real seconds (large import, its own thread pool)
        # for a value the suite only ever asserts is truthy. See that
        # module's `_skip_probe_requested` for the full rationale.
        "DARKFAC_SKIP_ACCELERATOR_PROBE": "1",
    }
    previous_offline_values = {
        key: os.environ.get(key) for key in offline_values
    }

    for key in _SECRET_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(offline_values)

    allow_network = request.config.getoption("--allow-network") or _env_flag(
        "DARKFAC_ALLOW_NETWORK"
    )
    original_urlopen = urllib.request.urlopen
    original_connect = socket.socket.connect

    def block_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("DarkFac official suite is offline")

    def block_connect(sock: socket.socket, address: Any) -> Any:
        """Block external TCP while preserving local test transports."""

        unix_family = getattr(socket, "AF_UNIX", None)
        if unix_family is not None and sock.family == unix_family:
            return original_connect(sock, address)
        if isinstance(address, tuple) and address:
            host = str(address[0]).strip("[]").lower()
            if host in {"localhost", "127.0.0.1", "::1"}:
                return original_connect(sock, address)
        raise OSError("DarkFac official suite is offline")

    if not allow_network:
        urllib.request.urlopen = block_urlopen  # type: ignore[assignment]
        socket.socket.connect = block_connect  # type: ignore[method-assign]

    try:
        yield
    finally:
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        for key, value in previous_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in previous_offline_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session", autouse=True)
def isolated_state_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Redirect DARKFAC_STATE_ROOT to an isolated tmp directory for the test session.

    This ensures that any test touching state_root() writes to an ephemeral
    directory instead of contaminating the repository's real .factory directory.
    """
    old_val = os.environ.get("DARKFAC_STATE_ROOT")
    state_dir = tmp_path_factory.mktemp("darkfac_state_root")
    os.environ["DARKFAC_STATE_ROOT"] = str(state_dir)
    # USR-190: the owner action backlog (.factory/owner_actions) is tracked data; code paths that
    # mirror line HumanRequests into it must never write to the real file during tests.
    old_owner_actions = os.environ.get("DARKFAC_OWNER_ACTIONS_PATH")
    os.environ["DARKFAC_OWNER_ACTIONS_PATH"] = str(state_dir / "owner_actions" / "owner_actions.json")
    try:
        yield state_dir
    finally:
        if old_val is None:
            os.environ.pop("DARKFAC_STATE_ROOT", None)
        else:
            os.environ["DARKFAC_STATE_ROOT"] = old_val
        if old_owner_actions is None:
            os.environ.pop("DARKFAC_OWNER_ACTIONS_PATH", None)
        else:
            os.environ["DARKFAC_OWNER_ACTIONS_PATH"] = old_owner_actions


@pytest.fixture(autouse=True)
def _isolate_ticket_execution_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give every test a private ticket state tree, including real stage tests.

    Ticket artifacts are keyed by ticket id. A session root lets concurrent
    tests using the same id race while resetting/writing SPEC.md, tickets.json,
    or progress.json. Tests that intentionally share state must opt in by
    creating it within their own test instead of relying on another test.
    """
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _isolate_operating_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing must not depend on which harness the developer running the tests operates through.

    A developer inside Claude Code has CLAUDECODE=1 (Grok Build exports GROK_AGENT, and so on); left in
    place, `core.line.operating_harness` would make `pick("development", ...)` prefer that harness and
    change results. A test that needs an operating harness sets the variables itself.
    """

    from core.line.operating_harness import AUTODETECT_ENV_SIGNALS, OPERATING_HARNESS_ENV

    for name in (OPERATING_HARNESS_ENV, *(name for name, _harness in AUTODETECT_ENV_SIGNALS)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _offline_ticket_stage_agents(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_ticket` PLAN/REVIEW call real planning/review agents (USR-375 S4); tests of other behaviour
    get a deterministic plan and an approving verdict instead. A module that exercises the stage
    contract itself sets `REAL_TICKET_STAGES = True` and injects its own doubles."""

    run_ticket = sys.modules.get("run_ticket")
    if run_ticket is None or getattr(request.module, "REAL_TICKET_STAGES", False):
        return
    # Ticket artifacts are keyed by ticket id. A session-wide state root lets parallel tests
    # with the same fixture ticket race in ticket_engine.reset(), deleting another test's PLAN.
    state_root = request.getfixturevalue("tmp_path") / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_root))
    from core.line import ticket_engine
    from core.line import workspace as line_workspace
    from core.line.stage_review import ReviewVerdict
    from core.line.stages import VERDICT_FILE
    from core.workflow.control_contracts import StageResult

    def plan(ticket: Any, _workspace: Any, rws: Any) -> None:
        ticket_engine.write_minimal_plan(rws, ticket)
        return None

    def review(_ticket: Any, _worktree: Any, rws: Any) -> StageResult:
        line_workspace.write_context(rws, VERDICT_FILE, ReviewVerdict(verdict="approve").model_dump_json())
        return StageResult(outcome="success", output_refs=["test"])

    monkeypatch.setattr(run_ticket, "_plan_with_agent", plan)
    monkeypatch.setattr(run_ticket, "_review_candidate", review)
    # RESEARCH never searches the web (no adapter) but would write the ledger of the main checkout (USR-376).
    from core.line import ticket_extras

    ledger = request.getfixturevalue("tmp_path_factory").mktemp("research_ledger")
    monkeypatch.setattr(ticket_extras, "research_ledger_root", lambda _main_root: ledger)


@pytest.fixture(autouse=True)
def _isolate_remote_worker_state_per_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker recovery state is private to one test, including under xdist.

    A suite-wide directory is insufficient: independent pytest workers can recover or terminate
    each other's persisted jobs when they construct a worker app without an explicit state path.
    Keep the directory stable within a test so restart/recovery tests still exercise persistence.
    """

    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))


@pytest.fixture(autouse=True)
def _isolate_fleet_env() -> Iterator[None]:
    """Fleet mode must not leak between tests (USR-369).

    ``run_ticket.py --fleet`` writes DARKFAC_FLEET_MODE straight into ``os.environ``, so a test that
    runs it in-process leaves fleet mode on for the next test in the same xdist worker, which then
    takes a per-host lock slot and a control-store reservation it never asked for. Saved and restored
    explicitly because ``monkeypatch.delenv`` does not undo a value set later outside monkeypatch.
    """

    names = ("DARKFAC_FLEET_MODE", "DARKFAC_HOST_ID")
    saved = {name: os.environ.pop(name) for name in names if name in os.environ}
    yield
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_node_liveness_cache() -> None:
    """The short liveness cache must not leak a node status between tests (USR-217)."""

    from core.infra import node_liveness

    node_liveness.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_remote_infra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from inheriting real worker addresses and probe settings.

    `core.harness.remote_dispatch.DEFAULT_WORKERS` points at REAL tailnet hosts (since 91fb348),
    so merely deleting `DARKFAC_REMOTE_HARNESS`/`DARKFAC_TEST_WORKERS` would make any unmocked
    call to `maybe_dispatch_remote`/`worker_urls`/`probe_health` probe or dispatch to them.
    Every test therefore starts in the safe mode `DARKFAC_REMOTE_HARNESS=off`; tests of the remote
    mode opt in with `monkeypatch.setenv(..., "auto")` (or `delenv`) and mock the network.
    """

    for name in AMBIENT_REMOTE_INFRA_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")


@pytest.fixture(autouse=True)
def _isolate_ambient_worker_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cloud-worker container settings must not leak into tests.

    - `DARKFAC_ROUTING_*` (the worker sets `DARKFAC_ROUTING_UNKNOWN_QUOTA=last_resort`) changes what
      `core.line.routing.pick` returns, so fail-closed assertions would fail inside the container.
    - `DARKFAC_ONPREM_BACKUP_DIR` (the env example points it at the Desktop's E: drive) decides where
      `CloudBackupService` mirrors encrypted backups; tests that build the service without arguments
      wrote them to the checkout (or to the real drive). Each test gets its own tmp mirror instead.

    A test that needs a specific value sets it itself (monkeypatch runs after this fixture).
    """

    for name in [key for key in os.environ if key.startswith("DARKFAC_ROUTING_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DARKFAC_ONPREM_BACKUP_DIR", str(tmp_path / "onprem_backup_mirror"))


@pytest.fixture(autouse=True)
def _isolate_delivery_policy_state(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep delivery tests independent of enterprise policies created by other tests.

    These tests exercise GitHub delivery with scripted ``gh`` runners. They must
    not inherit a project policy from another test running in the same xdist
    worker; policy-specific delivery behavior is covered by
    ``test_data_policy_enforcement_usr174.py`` with an explicit state root.
    """
    delivery_modules = {
        "test_git_autonomy_ci_gate.py",
        "test_git_autonomy_registry_usr185.py",
        "test_git_delivery.py",
        "test_queue_duplicate_registration.py",
        "test_queue_registration.py",
        "test_ticket_workspace.py",
    }
    if Path(str(request.node.path)).name in delivery_modules:
        monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "delivery_state"))


# Prefixes/names of external-service settings that a developer or worker machine legitimately exports
# (Telegram bots, Cloudflare R2 backup target, backup vault key) but that no offline test may inherit.
# With them set, `load_telegram_config` lets the environment win over the fixtures a test writes, and
# `R2StorageClient()` leaves its built-in mock mode and tries the real network (USR-163).
_EXTERNAL_SERVICE_ENV_PREFIXES = ("TELEGRAM_", "R2_")
_EXTERNAL_SERVICE_ENV_NAMES = ("DARKHUB_TELEGRAM_AUTO_WEBHOOK", "DARKFAC_BACKUP_ENCRYPTION_KEY")


@pytest.fixture(autouse=True)
def _isolate_external_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram and R2/backup settings of the host machine must never reach a test.

    - `TELEGRAM_*` in the process environment outranks `.factory/telegram/*.json` fixtures and the
      `TELEGRAM_ALLOWED_*` variables a test sets, so the same test passed or failed per machine.
    - `R2_*` makes `R2StorageClient()` (and so `CloudBackupService()` / `run_autonomous_backup_cycle`)
      go live; without them it uses its in-memory mock, which is what the offline suite needs.
    - `Telegram`'s repo-root `.env` fallback is ignored for the real checkout; a test that targets a
      tmp root (`root=...`/`DARKFAC_PROJECT_ROOT`) still reads that root's `.env` as before.

    A test that needs a value sets it itself (monkeypatch/mock.patch.dict run after this fixture).
    """

    for name in [
        key
        for key in os.environ
        if key.startswith(_EXTERNAL_SERVICE_ENV_PREFIXES) or key in _EXTERNAL_SERVICE_ENV_NAMES
    ]:
        monkeypatch.delenv(name, raising=False)

    from core.integrations import telegram as telegram_module

    real_read_env_fallback = telegram_module._read_env_fallback

    def read_env_fallback_without_real_checkout(root: Path | None = None) -> dict[str, str]:
        target = Path(root) if root is not None else telegram_module.project_root()
        try:
            if target.resolve() == IMPORT_ROOT.resolve():
                return {}
        except OSError:
            pass
        return real_read_env_fallback(root)

    monkeypatch.setattr(telegram_module, "_read_env_fallback", read_env_fallback_without_real_checkout)


@pytest.fixture(autouse=True)
def _never_sweep_the_real_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GitAutonomyManager.sweep_stale` must never run against the checkout that holds the tests.

    `run_ticket.resume_delivery` ends with `sweep_stale(cwd=PROJECT_ROOT)`. A test that drives
    `run_ticket.main` through a successful agent run reaches it in-process and would sweep the REAL
    repository: `git fetch --prune`, `gh pr list` per branch and, for every merged ticket/*, df/* or
    .claude/worktrees entry, `git worktree remove` with retries that sleep (via `time.sleep`, which the
    routing tests record). On a developer desktop there is nothing stale to remove so the test passes;
    inside the cloud worker container, leftover merged worktrees made the removal retry and
    `test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness` failed on `sleeps == []`.
    Sweeping a disposable repository (explicit tmp repo in the sweep tests) is unaffected, and a test
    that stubs `sweep_stale` itself overrides this guard.
    """

    from core.git.autonomy import GitAutonomyManager, SweepReport

    real_sweep = GitAutonomyManager.sweep_stale
    suite_root: list[Path] = []

    def guarded(self: Any, base: str = "main", gh_runner: Any = None, cwd: Path | None = None) -> Any:
        target = Path(cwd or self.root).resolve()
        if not suite_root:
            suite_root.append(GitAutonomyManager(IMPORT_ROOT)._main_root(IMPORT_ROOT)[0].resolve())
        if self._main_root(target)[0].resolve() == suite_root[0]:
            return SweepReport()
        return real_sweep(self, base, gh_runner, cwd)

    monkeypatch.setattr(GitAutonomyManager, "sweep_stale", guarded)


@pytest.fixture
def stereo_wav(tmp_path: Path) -> Path:
    """Generate a tiny deterministic stereo fixture without shipping binary data."""

    sample_rate = 8_000
    duration_sec = 1.0
    sample_count = int(sample_rate * duration_sec)
    timeline = np.arange(sample_count, dtype=np.float32) / sample_rate
    data = np.column_stack(
        (
            0.20 * np.sin(2 * np.pi * 220 * timeline),
            0.05 * np.sin(2 * np.pi * 440 * timeline),
        )
    ).astype(np.float32)
    path = tmp_path / "meeting_stereo.wav"
    sf.write(path, data, sample_rate, subtype="PCM_16")
    return path


@pytest.fixture(autouse=True)
def _no_real_gate_ram_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """USR-262: run_ticket's pre-gate RAM wait must never measure the real host or probe workers in tests."""
    run_ticket_module = sys.modules.get("run_ticket")
    if run_ticket_module is not None and hasattr(run_ticket_module, "_wait_gate_ram_if_local"):
        monkeypatch.setattr(run_ticket_module, "_wait_gate_ram_if_local", lambda as_json: None)
