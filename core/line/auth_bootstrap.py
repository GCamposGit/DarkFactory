"""Login bootstrap and auth probes for the VPS worker's agent CLIs (HF-27-09).

Two problems this module solves without SSH access to the VPS:

1. **Probing** whether `claude`/`codex` are actually authenticated, so
   `core.orchestrator.cloud_worker.CloudWorker` can drop a `harness:x`
   capability it cannot honor instead of claiming a job that will fail
   (`filter_capabilities_by_auth` / `probe_harness_auth`).
2. **Bootstrapping** a login when the probe fails, per
   `docs/PRODUCTION_LINE_PLAN_2026-09-22.md` section 7, items 1-2:
   - Codex supports unattended device-code login (`codex login
     --device-auth`); this module runs it, extracts the URL and code
     from the CLI output, and relays them to the owner over Telegram.
   - Claude Code has no headless device-auth flow; the owner must run
     `claude setup-token` on a machine with a browser and paste the
     resulting token into the Dokploy worker's `CLAUDE_CODE_OAUTH_TOKEN`
     environment variable. This module sends that instruction and then
     probes current status.

Entry point: `python -m core.line.auth_bootstrap <codex|claude>`.

No network call is made unless a probe/login subprocess or the Telegram
gateway is reached; every failure mode (missing binary, timeout, no
Telegram chat configured) degrades to a `HarnessProbeResult(ok=False)`
instead of raising, so this module is safe to call from the worker's
boot path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from pydantic import BaseModel

from core.harness.remote_worker import find_codex_binary
from core.line.agent_cli import AgentRequest, run_agent

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

_URL_PATTERN = re.compile(r"https?://\S+")
_DEVICE_CODE_PATTERN = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5})\b")
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class HarnessProbeResult(BaseModel):
    """Outcome of an auth probe or a login-bootstrap attempt for one harness."""

    harness: str
    ok: bool
    detail: str = ""


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def probe_claude(timeout_s: int = 60) -> HarnessProbeResult:
    """Probe Claude Code auth by running `claude -p ok` in read mode.

    Reuses `core.line.agent_cli.run_agent` (HF-27-03) so the argv, binary
    discovery and error classification (`auth_expired`, `not_installed`,
    `timeout`) stay in one place.
    """
    result = run_agent(
        AgentRequest(prompt="ok", cwd=REPO_ROOT, mode="read", harness="claude", timeout_s=timeout_s)
    )
    if result.error_kind in ("not_installed", "auth_expired"):
        return HarnessProbeResult(harness="claude", ok=False, detail=result.error_kind or "")
    if not result.ok:
        return HarnessProbeResult(harness="claude", ok=False, detail=(result.text or "")[:200])
    return HarnessProbeResult(harness="claude", ok=True, detail="ok")


def probe_codex_status(timeout_s: int = 30) -> HarnessProbeResult:
    """Probe Codex auth by running `codex login status`."""
    executable = find_codex_binary()
    if not executable:
        return HarnessProbeResult(harness="codex", ok=False, detail="not_installed")
    try:
        proc = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return HarnessProbeResult(harness="codex", ok=False, detail="timeout")
    except OSError as exc:
        return HarnessProbeResult(harness="codex", ok=False, detail=str(exc))

    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    ok = proc.returncode == 0 and "not logged in" not in combined and "logged out" not in combined
    return HarnessProbeResult(harness="codex", ok=ok, detail=combined.strip()[:200])


_PROBES: dict[str, Callable[[], HarnessProbeResult]] = {
    "claude": probe_claude,
    "codex": probe_codex_status,
}


def probe_harness_auth(harness: str) -> bool:
    """True if `harness` has a registered probe that passes.

    A harness with no registered probe (e.g. `grok`, `antigravity`, which
    have no device-auth story yet) is assumed OK so this never drops a
    capability it cannot actually evaluate. Never raises.
    """
    probe = _PROBES.get(harness)
    if probe is None:
        return True
    try:
        return probe().ok
    except Exception as exc:  # pragma: no cover - defensive, probes already trap their own errors
        logger.warning("Auth probe for harness %s raised: %s", harness, exc)
        return False


def filter_capabilities_by_auth(
    capabilities: list[str],
    prober: Callable[[str], bool] = probe_harness_auth,
) -> list[str]:
    """Drop `harness:<name>` entries whose auth probe fails; keep everything else.

    Used by `core.orchestrator.cloud_worker.CloudWorker` at boot so the VPS
    never publishes a `harness:x` capability it cannot honor (HF-27-09
    acceptance: "o probe de auth falho no boot remove harness:x das caps
    publicadas").
    """
    kept: list[str] = []
    for cap in capabilities:
        if cap.startswith("harness:"):
            harness_name = cap.split(":", 1)[1]
            try:
                ok = prober(harness_name)
            except Exception as exc:
                logger.warning("Auth probe for %s raised; dropping capability: %s", cap, exc)
                ok = False
            if not ok:
                logger.warning("Dropping capability %s: auth probe failed", cap)
                continue
        kept.append(cap)
    return kept


# --------------------------------------------------------------------------
# Human notification (Telegram) — best-effort, never raises
# --------------------------------------------------------------------------


def _notify_human(message: str, notifier: Optional[Callable[[str], bool]] = None) -> bool:
    """Send `message` to the owner's ops chat. Defaults to the Telegram gateway."""
    if notifier is not None:
        try:
            return notifier(message)
        except Exception as exc:
            logger.warning("Custom notifier failed: %s", exc)
            return False
    try:
        from core.integrations.telegram import TelegramGateway

        config = _load_notify_config()
        if config is None:
            logger.warning("No Telegram chat configured for auth_bootstrap HumanRequest: %s", message)
            return False
        gateway = TelegramGateway(config)
        return gateway.send_message(config.authorized_chat_ids[0], message)
    except Exception as exc:
        logger.warning("Failed to notify human via Telegram: %s", exc)
        return False


def _load_notify_config():  # type: ignore[no-untyped-def]
    """First Telegram config (ops, then owner) that has a chat to write to.

    The cloud worker container only receives the OWNER bot env
    (TELEGRAM_OWNER_BOT_TOKEN/_AUTHORIZED_*), so an ops-only lookup found no
    chat there and every bootstrap message was silently dropped.
    """
    from core.integrations.telegram import load_telegram_config

    for role in ("ops", "owner"):
        try:
            config = load_telegram_config(role=role)
        except Exception as exc:
            logger.debug("Telegram config for role %s unavailable: %s", role, exc)
            continue
        if config.authorized_chat_ids:
            return config
    return None


def telegram_configured() -> bool:
    """True if some Telegram role has a chat the bootstrap message can reach."""
    try:
        return _load_notify_config() is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# Login bootstrap (HumanRequest(kind=account) equivalents, plan section 7 #1-2)
# --------------------------------------------------------------------------


def bootstrap_codex_login(
    notifier: Optional[Callable[[str], bool]] = None,
    timeout_s: int = 60,
    approval_wait_s: int = 900,
) -> HarnessProbeResult:
    """Run `codex login --device-auth`, relay URL+code to the owner, then probe status.

    `codex login --device-auth` prints the URL and code and then keeps
    polling until the owner approves (or the code expires), so it does not
    exit on its own. It is therefore streamed with `Popen`: the URL/code are
    read as soon as they appear (up to `timeout_s`) and relayed *before*
    waiting up to `approval_wait_s` for the process to finish. (The previous
    `subprocess.run(..., timeout=60)` version raised `TimeoutExpired` on the
    first real run and never sent anything.)

    Never raises: a missing binary, a failed spawn, or output the regexes
    cannot parse all fold into a failed `HarnessProbeResult` with a
    human-readable `detail`, instead of propagating an exception into the
    worker boot path.
    """
    executable = find_codex_binary()
    if not executable:
        return HarnessProbeResult(harness="codex", ok=False, detail="not_installed")
    try:
        proc = subprocess.Popen(
            [executable, "login", "--device-auth"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return HarnessProbeResult(harness="codex", ok=False, detail=str(exc))

    chunks: list[str] = []
    lock = threading.Lock()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                with lock:
                    chunks.append(line)
        except Exception:  # pragma: no cover - reader must never raise
            pass

    reader = threading.Thread(target=_reader, daemon=True, name="codex-device-auth-reader")
    reader.start()

    def _snapshot() -> str:
        with lock:
            return _ANSI_PATTERN.sub("", "".join(chunks))

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        text = _snapshot()
        if (_URL_PATTERN.search(text) and _DEVICE_CODE_PATTERN.search(text)) or proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is not None:
        reader.join(timeout=2.0)

    combined = _snapshot()
    url_match = _URL_PATTERN.search(combined)
    code_match = _DEVICE_CODE_PATTERN.search(combined)
    lines = [
        "[DarkFac] Codex precisa de login (device-auth) na VPS.",
        f"1. Abra: {url_match.group(0) if url_match else '(URL nao encontrada na saida do CLI; rode codex login --device-auth manualmente)'}",
        f"2. Digite o codigo: {code_match.group(1) if code_match else '(codigo nao encontrado na saida do CLI)'}",
        "3. Aprove o dispositivo na conta ChatGPT usada pela fabrica.",
        "A fabrica vai sondar 'codex login status' apos a aprovacao.",
    ]
    _notify_human("\n".join(lines), notifier)

    try:
        proc.wait(timeout=approval_wait_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill is best effort
            pass
    return probe_codex_status()


def bootstrap_claude_login(
    notifier: Optional[Callable[[str], bool]] = None,
) -> HarnessProbeResult:
    """Explain the `claude setup-token` -> Dokploy env flow and probe current status.

    Claude Code has no unattended device-auth flow, so this never runs a
    login subprocess: it only sends instructions and probes what is
    currently configured.
    """
    lines = [
        "[DarkFac] Claude Code precisa de um token de assinatura na VPS.",
        "1. No Desktop (ou outra maquina com navegador), rode: claude setup-token",
        "2. Copie o token exibido (comeca com 'sk-ant-oat...').",
        "3. No Dokploy, abra o servico darkfac-worker > Environment e cole em CLAUDE_CODE_OAUTH_TOKEN.",
        "4. Redeploy o servico. A fabrica vai sondar 'claude -p ok' apos o redeploy.",
    ]
    _notify_human("\n".join(lines), notifier)
    return probe_claude()


CODEX_LOGIN_MIN_INTERVAL_S = 6 * 3600
_STAMP_NAME = ".darkfac_codex_login_stamp"


def default_codex_stamp_path() -> Path:
    """Stamp file inside the persistent Codex auth volume (`/home/darkfac/.codex`)."""
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / _STAMP_NAME


class CodexLoginTrigger:
    """Starts `bootstrap_codex_login` in a background thread, at most once per window.

    Called from the worker's boot path and after each capability re-probe
    with the worker's current capability list. It only acts when
    `harness:codex` is *expected* (configured/installed) but missing, and a
    Telegram chat exists to deliver the device code. The throttle stamp lives
    in the persistent auth volume so restarts/redeploys do not re-spam the
    owner. Never blocks or raises.

    `clock`, `bootstrap`, `telegram_ok`, `spawn` and `stamp_path` are
    injectable so it is testable without threads, time or a real CLI.
    """

    def __init__(
        self,
        *,
        stamp_path: Optional[Path] = None,
        min_interval_s: float = CODEX_LOGIN_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        bootstrap: Optional[Callable[[], HarnessProbeResult]] = None,
        telegram_ok: Optional[Callable[[], bool]] = None,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.stamp_path = stamp_path or default_codex_stamp_path()
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._bootstrap = bootstrap or bootstrap_codex_login
        self._telegram_ok = telegram_ok or telegram_configured
        self._spawn = spawn or self._spawn_thread
        self._last_started: Optional[float] = None
        self._running = False

    @staticmethod
    def _spawn_thread(target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True, name="codex-login-bootstrap").start()

    def _stamp_age_s(self) -> Optional[float]:
        try:
            return self._clock() - float(self.stamp_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _write_stamp(self) -> None:
        try:
            self.stamp_path.parent.mkdir(parents=True, exist_ok=True)
            self.stamp_path.write_text(str(self._clock()), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write codex login stamp %s: %s", self.stamp_path, exc)

    def _run(self) -> None:
        try:
            result = self._bootstrap()
            logger.info("Codex login bootstrap finished: ok=%s detail=%s", result.ok, result.detail)
        except Exception as exc:  # never crash the worker from the background thread
            logger.warning("Codex login bootstrap failed: %s", exc)
        finally:
            self._running = False

    def maybe_start(self, capabilities: Iterable[str], *, codex_expected: bool = True) -> bool:
        """True if a bootstrap thread was started by this call."""
        try:
            if not codex_expected or "harness:codex" in set(capabilities):
                return False
            if self._running:
                return False
            if not self._telegram_ok():
                return False
            now = self._clock()
            if self._last_started is not None and now - self._last_started < self.min_interval_s:
                return False
            age = self._stamp_age_s()
            if age is not None and 0 <= age < self.min_interval_s:
                return False
            # Stamp BEFORE starting: a crash/timeout must not turn into a retry storm.
            self._last_started = now
            self._write_stamp()
            self._running = True
            self._spawn(self._run)
            return True
        except Exception as exc:
            logger.warning("Codex login trigger failed: %s", exc)
            self._running = False
            return False


_BOOTSTRAP: dict[str, Callable[..., HarnessProbeResult]] = {
    "codex": bootstrap_codex_login,
    "claude": bootstrap_claude_login,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory agent-CLI auth bootstrap (HF-27-09)")
    parser.add_argument("harness", choices=sorted(_BOOTSTRAP), help="Harness to bootstrap login for")
    args = parser.parse_args(argv)

    bootstrap = _BOOTSTRAP[args.harness]
    result = bootstrap()
    sys.stdout.write(json.dumps(result.model_dump(), ensure_ascii=False) + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
