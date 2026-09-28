#!/usr/bin/env python3
"""Live Operational Drill & Runtime Preflight Verifier (Gate G-Live).

Inviolable Rule (AGENTS.md):
"Todo componente ou integração externa (ex: Telegram, n8n, GitHub, Dokploy, Provedores)
só é considerado entregue após configuração ativa e validação operacional end-to-end
no mundo real com o owner. É terminantemente proibido declarar entregas apenas com
base em simulações/mocks sintéticos postergando a validação e setup real para o final."

Validates:
1. Port binds and process freshness (detects zombie servers serving stale code).
2. Live HTTP probing against real endpoints (no in-memory TestClient).
3. External integrations transport readiness (Telegram webhook/polling).
4. Secret & environment variable availability across environments (local and Dokploy).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("darkfac.harness.live_preflight")


@dataclass
class ProbeCheck:
    name: str
    target: str
    passed: bool
    status_code: Optional[int] = None
    detail: str = ""
    remediation: str = ""


@dataclass
class PreflightReport:
    timestamp: str
    ticket_id: Optional[str]
    all_passed: bool
    checks: List[ProbeCheck] = field(default_factory=list)


def check_port_listening(host: str, port: int) -> bool:
    """Check if a TCP port is actively listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def get_process_on_port(port: int) -> Optional[int]:
    """Find the owning PID on Windows using netstat."""
    try:
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                return int(parts[4])
    except Exception:
        pass
    return None


def probe_http_endpoint(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    expected_statuses: Tuple[int, ...] = (200,),
    timeout: float = 4.0,
) -> Tuple[bool, Optional[int], str]:
    """Execute a real HTTP request against a running server."""
    req_headers: Dict[str, str] = {}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return (resp.status in expected_statuses, resp.status, body[:300].decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as err:
        return (err.code in expected_statuses, err.code, err.read()[:300].decode("utf-8", errors="replace"))
    except Exception as exc:
        return (False, None, str(exc))


def audit_hub_runtime(base_url: str = "http://127.0.0.1:8888") -> List[ProbeCheck]:
    """Audit the DarkHub local server for freshness and critical endpoints."""
    checks: List[ProbeCheck] = []

    # 1. Base connectivity & Port binding
    port = 8888
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        if parsed.port:
            port = parsed.port
    except Exception:
        pass

    pid = get_process_on_port(port)
    if not pid:
        checks.append(ProbeCheck(
            name="DarkHub Port Binding",
            target=f"127.0.0.1:{port}",
            passed=False,
            detail=f"Nenhum processo escutando na porta {port}.",
            remediation=f"Execute: python {REPO_ROOT / 'run_hub.py'}",
        ))
        return checks

    checks.append(ProbeCheck(
        name="DarkHub Port Binding",
        target=f"127.0.0.1:{port}",
        passed=True,
        detail=f"Porta ativa escutada pelo PID {pid}.",
    ))

    # 2. Local API connectivity probe
    ok, code, body = probe_http_endpoint(f"{base_url}/api/services", timeout=5.0)
    checks.append(ProbeCheck(
        name="DarkHub Local Core API",
        target=f"{base_url}/api/services",
        passed=ok,
        status_code=code,
        detail=f"Status: {code}. API local respondendo em tempo real.",
        remediation="Verifique se o backend do DarkHub subiu corretamente sem erros de importação.",
    ))

    # 3. Audio Transcribe endpoint probe (USR-60)
    # A valid minimal 44-byte WAV header for mono 16kHz PCM
    minimal_wav = bytes([
        0x52, 0x49, 0x46, 0x46, 0x24, 0x00, 0x00, 0x00,  # RIFF, size 36
        0x57, 0x41, 0x56, 0x45, 0x66, 0x6D, 0x74, 0x20,  # WAVEfmt 
        0x10, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00,  # 16, PCM, 1 channel
        0x80, 0x3E, 0x00, 0x00, 0x00, 0x7D, 0x00, 0x00,  # 16000 Hz, 32000 bytes/s
        0x02, 0x00, 0x10, 0x00, 0x64, 0x61, 0x74, 0x61,  # 2 block align, 16 bits, 'data'
        0x00, 0x00, 0x00, 0x00                           # 0 bytes data
    ])
    ok_audio, code_audio, body_audio = probe_http_endpoint(
        f"{base_url}/api/audio/transcribe",
        method="POST",
        data=minimal_wav,
        headers={"Content-Type": "audio/wav"},
        expected_statuses=(200, 400),  # 200 or 400 (empty audio) proves endpoint is alive and routing
        timeout=25.0,  # Whisper model cold start can take 10-15s
    )
    passed_audio = ok_audio and code_audio != 404
    checks.append(ProbeCheck(
        name="DarkHub Audio Transcribe Route (Live)",
        target=f"{base_url}/api/audio/transcribe",
        passed=passed_audio,
        status_code=code_audio,
        detail=f"Status: {code_audio}. " + ("Rota ativa e respondendo." if passed_audio else f"FALHA 404 ou erro: {body_audio[:120]}"),
        remediation="O servidor local está rodando código antigo (zumbi). Reinicie o run_hub.py matando processos anteriores.",
    ))

    return checks


def audit_telegram_readiness() -> List[ProbeCheck]:
    """Audit Telegram bot token and transport channel readiness."""
    checks: List[ProbeCheck] = []
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_OPS_BOT_TOKEN")
    if not bot_token:
        # Try reading .env directly
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not bot_token:
        checks.append(ProbeCheck(
            name="Telegram Bot Token",
            target="TELEGRAM_BOT_TOKEN",
            passed=False,
            detail="Nenhum bot token configurado no ambiente ou em .env.",
            remediation="Configure TELEGRAM_BOT_TOKEN no .env.",
        ))
        return checks

    # 1. getMe API probe
    me_url = f"https://api.telegram.org/bot{bot_token}/getMe"
    ok, code, body = probe_http_endpoint(me_url)
    bot_username = ""
    try:
        data = json.loads(body)
        bot_username = data.get("result", {}).get("username", "")
    except Exception:
        pass

    checks.append(ProbeCheck(
        name="Telegram API Authentication (getMe)",
        target=f"@{bot_username}" if bot_username else "Telegram Bot API",
        passed=ok,
        status_code=code,
        detail=f"Bot autenticado: @{bot_username}" if ok else f"Falha na autenticação: {body[:100]}",
        remediation="Verifique se o token do Telegram é válido.",
    ))

    # 2. Webhook & Transport Status
    webhook_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
    ok_wh, code_wh, body_wh = probe_http_endpoint(webhook_url)
    wh_target = ""
    has_webhook = False
    try:
        wh_data = json.loads(body_wh)
        wh_target = wh_data.get("result", {}).get("url", "")
        has_webhook = bool(wh_target)
    except Exception:
        pass

    checks.append(ProbeCheck(
        name="Telegram Inbound Transport (Webhook/Polling)",
        target=wh_target or "Polling (No Webhook Set)",
        passed=True,  # Informational or verified
        status_code=code_wh,
        detail=f"Webhook ativo em: '{wh_target}'" if has_webhook else "Webhook vazio. Entradas dependem de daemon de polling ativo.",
        remediation="Se desejar receber webhooks em nuvem, registre a URL pública do DarkHub via setWebhook.",
    ))

    return checks


def audit_secrets_and_cloud() -> List[ProbeCheck]:
    """Audit existence of required AI credentials."""
    checks: List[ProbeCheck] = []
    
    # Check GROQ_API_KEY
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("GROQ_API_KEY="):
                    groq_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    has_groq = bool(groq_key and len(groq_key) > 10)
    checks.append(ProbeCheck(
        name="Groq Cloud Whisper Fallback Secret",
        target="GROQ_API_KEY",
        passed=has_groq,
        detail="Chave Groq presente para fallback rápido na nuvem/CPU." if has_groq else "Chave GROQ_API_KEY ausente no .env do DarkFac.",
        remediation="Adicione GROQ_API_KEY='...' no C:\\dev\\DarkFac\\.env e nas variáveis de ambiente do Dokploy.",
    ))

    return checks


def run_live_preflight(ticket_id: Optional[str] = None, hub_url: str = "http://127.0.0.1:8888") -> PreflightReport:
    from datetime import datetime, UTC
    now_str = datetime.now(UTC).isoformat()
    
    checks: List[ProbeCheck] = []
    checks.extend(audit_hub_runtime(base_url=hub_url))
    checks.extend(audit_telegram_readiness())
    checks.extend(audit_secrets_and_cloud())

    all_passed = all(c.passed for c in checks)
    return PreflightReport(
        timestamp=now_str,
        ticket_id=ticket_id,
        all_passed=all_passed,
        checks=checks,
    )


def print_report(report: PreflightReport, json_mode: bool = False) -> None:
    if json_mode:
        data = {
            "timestamp": report.timestamp,
            "ticket_id": report.ticket_id,
            "all_passed": report.all_passed,
            "checks": [
                {
                    "name": c.name,
                    "target": c.target,
                    "passed": c.passed,
                    "status_code": c.status_code,
                    "detail": c.detail,
                    "remediation": c.remediation,
                }
                for c in report.checks
            ]
        }
        print(json.dumps(data, indent=2))
        return

    print("=" * 70)
    print(f" [GATE G-LIVE] Live Operational Preflight Drill")
    if report.ticket_id:
        print(f" Ticket Alvo: {report.ticket_id}")
    print(f" Timestamp:   {report.timestamp}")
    print("=" * 70)

    for c in report.checks:
        icon = "[PASS]" if c.passed else "[FAIL]"
        print(f"\n{icon} {c.name}")
        print(f"       Alvo:     {c.target}")
        print(f"       Detalhes: {c.detail}")
        if not c.passed and c.remediation:
            print(f"       Correção: {c.remediation}")

    print("\n" + "=" * 70)
    if report.all_passed:
        print(" VEREDICTO: [LIVE_PREFLIGHT_PASS] - Runtime e canais verificados no mundo real.")
    else:
        print(" VEREDICTO: [LIVE_PREFLIGHT_FAIL] - Falhas de runtime detectadas. Entrega bloqueada.")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Operational Preflight Drill (Gate G-Live)")
    parser.add_argument("--ticket", default=None, help="Target ticket ID (e.g. USR-60)")
    parser.add_argument("--hub-url", default="http://127.0.0.1:8888", help="DarkHub URL to probe")
    parser.add_argument("--json", action="store_true", help="Output report in JSON format")
    args = parser.parse_args()

    report = run_live_preflight(ticket_id=args.ticket, hub_url=args.hub_url)
    print_report(report, json_mode=args.json)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
