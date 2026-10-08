"""Shared deterministic fixtures and safety rails for the official suite."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

if "DARKFAC_STATE_ROOT" not in os.environ:
    _EARLY_STATE_ROOT = tempfile.mkdtemp(prefix="darkfac_state_")
    os.environ["DARKFAC_STATE_ROOT"] = _EARLY_STATE_ROOT

import numpy as np
import pytest
import soundfile as sf

IMPORT_ROOT = Path(__file__).resolve().parent.parent
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.harness import suite_lock as _suite_lock
from tests import _tree_hygiene, _worker_capacity

# Mitigate Windows WMI query crashes (0x8007000e) during worker startup (USR-95)
_worker_capacity.mitigate_windows_wmi_startup()

_DEFAULT_SUITE_LOCK_MIN_ITEMS = 100

# Set by pytest_sessionstart (xdist controller) or pytest_collection_finish
# (plain, non-distributed run with enough items to count as "the full
# suite"). Released in pytest_sessionfinish. A raw `pytest` invocation by ANY
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


def pytest_sessionstart(session: pytest.Session) -> None:
    """xdist controller acquires the machine-wide lock before workers spawn."""

    config = session.config
    if not _lock_should_be_skipped(config) and _requested_numprocesses(config) is not None:
        _acquire_session_lock()
    _start_tree_guard(config)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Non-distributed run: only queue once collection proves this is a big run."""

    config = session.config
    if _lock_should_be_skipped(config):
        return
    if _session_suite_lock is not None:
        return  # already acquired at sessionstart (xdist controller path)
    if _requested_numprocesses(config) is not None:
        return  # xdist requested but sessionstart already handled it
    min_items = int(os.environ.get("DARKFAC_SUITE_LOCK_MIN_ITEMS", str(_DEFAULT_SUITE_LOCK_MIN_ITEMS)))
    if len(session.items) >= min_items:
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
    try:
        yield state_dir
    finally:
        if old_val is None:
            os.environ.pop("DARKFAC_STATE_ROOT", None)
        else:
            os.environ["DARKFAC_STATE_ROOT"] = old_val


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
