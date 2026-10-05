#!/usr/bin/env python3
"""Telegram Token Rotation & Verification Tool (USR-110).

Inspects all local token sources (.env, os.environ, .factory/telegram/*.json),
validates getMe and getWebhookInfo against the Telegram Bot API, and outputs
status reports containing ONLY masked fingerprints (NEVER full tokens).

Usage:
    python scripts/telegram_rotate_check.py
    python scripts/telegram_rotate_check.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.telegram.org"


def mask_token(token: str | None) -> str:
    """Produce a safe fingerprint without revealing credentials."""
    if not token:
        return "(vazio)"
    clean = token.strip()
    if len(clean) > 8:
        return f"{clean[:4]}...{clean[-4:]}"
    return "***"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file safely."""
    if not path.is_file():
        return {}
    res: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        res[key] = val
    return res


def probe_telegram_api(token: str, timeout: float = 5.0) -> dict[str, Any]:
    """Perform getMe and getWebhookInfo probes without leaking token in exceptions."""
    result: dict[str, Any] = {
        "get_me_ok": False,
        "bot_username": None,
        "bot_id": None,
        "get_me_error": None,
        "webhook_ok": False,
        "webhook_url": None,
        "pending_updates": None,
        "webhook_error": None,
    }

    # 1. Probe getMe
    get_me_url = f"{API_BASE}/bot{token}/getMe"
    try:
        req = urllib.request.Request(get_me_url, headers={"User-Agent": "DarkFactory-Check/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                result["get_me_ok"] = True
                result["bot_username"] = data.get("result", {}).get("username")
                result["bot_id"] = data.get("result", {}).get("id")
            else:
                result["get_me_error"] = f"Telegram error: {data.get('description', 'not ok')}"
    except urllib.error.HTTPError as exc:
        result["get_me_error"] = f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        result["get_me_error"] = type(exc).__name__

    # 2. Probe getWebhookInfo
    wh_url = f"{API_BASE}/bot{token}/getWebhookInfo"
    try:
        req = urllib.request.Request(wh_url, headers={"User-Agent": "DarkFactory-Check/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                result["webhook_ok"] = True
                wh_res = data.get("result", {})
                result["webhook_url"] = wh_res.get("url") or "(nenhuma / polling ativo)"
                result["pending_updates"] = wh_res.get("pending_update_count", 0)
            else:
                result["webhook_error"] = f"Telegram error: {data.get('description', 'not ok')}"
    except urllib.error.HTTPError as exc:
        result["webhook_error"] = f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        result["webhook_error"] = type(exc).__name__

    return result


def collect_sources(root_dir: Path = REPO_ROOT) -> list[dict[str, Any]]:
    """Scan all local files and environment variables for configured bot tokens."""
    sources: list[dict[str, Any]] = []

    # 1. os.environ
    for key in ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_BOT_TOKEN"):
        val = os.environ.get(key)
        if val:
            role = "owner" if "OWNER" in key else "ops"
            sources.append({
                "source": "os.environ",
                "key": key,
                "role": role,
                "token": val.strip(),
            })

    # 2. .env
    env_file = root_dir / ".env"
    if env_file.exists():
        parsed = read_env_file(env_file)
        for key in ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_BOT_TOKEN"):
            val = parsed.get(key)
            if val:
                role = "owner" if "OWNER" in key else "ops"
                sources.append({
                    "source": ".env",
                    "key": key,
                    "role": role,
                    "token": val.strip(),
                })

    # 3. .factory/telegram/*.json
    tg_dir = root_dir / ".factory" / "telegram"
    for fname in ("owner_config.json", "ops_config.json", "config.json"):
        fpath = tg_dir / fname
        if fpath.exists():
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                val = data.get("bot_token")
                if val:
                    role = "owner" if "owner" in fname else "ops"
                    sources.append({
                        "source": f".factory/telegram/{fname}",
                        "key": "bot_token",
                        "role": role,
                        "token": str(val).strip(),
                    })
            except Exception:
                pass

    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP probe timeout in seconds (default: 5.0)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sources = collect_sources()
    if not sources:
        if args.json:
            print(json.dumps({"sources": [], "status": "no_tokens_found"}))
        else:
            print("[!] Nenhuma fonte de token do Telegram encontrada nos caminhos locais.")
        return 0

    results: list[dict[str, Any]] = []
    all_ok = True

    for item in sources:
        probe = probe_telegram_api(item["token"], timeout=args.timeout)
        fingerprint = mask_token(item["token"])
        entry = {
            "source": item["source"],
            "key": item["key"],
            "role": item["role"],
            "fingerprint": fingerprint,
            "get_me_ok": probe["get_me_ok"],
            "bot_username": probe["bot_username"],
            "bot_id": probe["bot_id"],
            "get_me_error": probe["get_me_error"],
            "webhook_ok": probe["webhook_ok"],
            "webhook_url": probe["webhook_url"],
            "pending_updates": probe["pending_updates"],
            "webhook_error": probe["webhook_error"],
        }
        results.append(entry)
        if not probe["get_me_ok"]:
            all_ok = False

    if args.json:
        print(json.dumps({"sources": results, "all_valid": all_ok}, indent=2))
        return 0 if all_ok else 1

    print("=" * 80)
    print(" VERIFICAÇÃO DE TOKENS DO TELEGRAM (USR-110)")
    print("=" * 80)
    for r in results:
        status_label = f"OK (@{r['bot_username']}, id={r['bot_id']})" if r["get_me_ok"] else f"FALHA ({r['get_me_error']})"
        wh_label = f"{r['webhook_url']} (pending={r['pending_updates']})" if r["webhook_ok"] else f"FALHA ({r['webhook_error']})"
        print(f"\nFonte:       {r['source']} [{r['key']}]")
        print(f"Papel:       {r['role']}")
        print(f"Fingerprint: {r['fingerprint']}")
        print(f"getMe:       {status_label}")
        print(f"Webhook:     {wh_label}")

    print("\n" + "-" * 80)
    if all_ok:
        print("[PASS] Todos os tokens locais configurados são válidos perante a API do Telegram.")
        return 0
    else:
        print("[WARN] Um ou mais tokens locais falharam na autenticação (possível token revogado/antigo).")
        print("Consulte o runbook em docs/runbooks/telegram_rotation.md para realizar a rotação correta.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
