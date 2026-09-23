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
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from core.harness.remote_worker import find_codex_binary
from core.line.agent_cli import AgentRequest, run_agent

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

_URL_PATTERN = re.compile(r"https?://\S+")
_DEVICE_CODE_PATTERN = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")


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
        from core.integrations.telegram import TelegramGateway, load_telegram_config

        config = load_telegram_config(role="ops")
        chat_ids = config.authorized_chat_ids
        if not chat_ids:
            logger.warning("No Telegram chat configured for auth_bootstrap HumanRequest: %s", message)
            return False
        gateway = TelegramGateway(config)
        return gateway.send_message(chat_ids[0], message)
    except Exception as exc:
        logger.warning("Failed to notify human via Telegram: %s", exc)
        return False


# --------------------------------------------------------------------------
# Login bootstrap (HumanRequest(kind=account) equivalents, plan section 7 #1-2)
# --------------------------------------------------------------------------


def bootstrap_codex_login(
    notifier: Optional[Callable[[str], bool]] = None,
    timeout_s: int = 60,
) -> HarnessProbeResult:
    """Run `codex login --device-auth`, relay URL+code to the owner, then probe status.

    Never raises: a missing binary, a subprocess timeout, or output the
    regexes cannot parse all fold into a failed `HarnessProbeResult` with a
    human-readable `detail`, instead of propagating an exception into the
    worker boot path.
    """
    executable = find_codex_binary()
    if not executable:
        return HarnessProbeResult(harness="codex", ok=False, detail="not_installed")
    try:
        proc = subprocess.run(
            [executable, "login", "--device-auth"],
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

    combined = f"{proc.stdout}\n{proc.stderr}"
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
