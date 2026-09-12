#!/usr/bin/env python3
"""Headless CLI for Telegram Gateway and n8n Community integration (HF-14).

Provides deterministic headless automation commands with full --json support
and UTF-8 safe output for Windows PowerShell.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.integrations.n8n import (
    N8nApiClient,
    N8nConfig,
    N8nManifestGenerator,
    N8nProbe,
    N8nWorkflowManager,
    load_n8n_config,
)
from core.integrations.telegram import (
    TelegramConfig,
    TelegramGateway,
    redact_secrets,
)


def _load_telegram_config(config_file: Optional[Path] = None) -> TelegramConfig:
    cfg_path = config_file or (REPO_ROOT / ".factory" / "telegram" / "config.json")
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return TelegramConfig.model_validate(data)
        except Exception:
            pass

    # Read from environment variables if present
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    users = [int(u.strip()) for u in os.environ.get("TELEGRAM_AUTHORIZED_USERS", "").split(",") if u.strip().isdigit()]
    chats = [int(c.strip()) for c in os.environ.get("TELEGRAM_AUTHORIZED_CHATS", "").split(",") if c.strip().isdigit()]
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    return TelegramConfig(
        bot_token=token,
        authorized_user_ids=users,
        authorized_chat_ids=chats,
        webhook_secret_token=secret,
    )


def cmd_telegram_process_update(args: argparse.Namespace) -> int:
    """Process a raw Telegram update JSON."""
    try:
        if args.payload.startswith("@") or Path(args.payload).exists():
            path = Path(args.payload[1:] if args.payload.startswith("@") else args.payload)
            update_data = json.loads(path.read_text(encoding="utf-8"))
        else:
            update_data = json.loads(args.payload)
    except Exception as exc:
        err_obj = {"error": f"Failed to parse payload JSON: {exc}"}
        if args.json:
            print(json.dumps(err_obj, indent=2))
        else:
            print(f"Error: {err_obj['error']}", file=sys.stderr)
        return 2

    config = _load_telegram_config(args.config)
    if args.user_id:
        config.authorized_user_ids.append(args.user_id)

    gateway = TelegramGateway(config=config, state_dir=REPO_ROOT / ".factory" / "telegram")
    result = gateway.process_update(update_data)

    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(f"Update {result.update_id}: action={result.action.value}, authorized={result.authorized}, duplicate={result.duplicate}")
        if result.response_text:
            print(f"Response: {result.response_text}")
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)

    return 0 if result.authorized and not result.error else 1


def cmd_telegram_status(args: argparse.Namespace) -> int:
    """Check Telegram gateway status."""
    config = _load_telegram_config(args.config)
    gateway = TelegramGateway(config=config, state_dir=REPO_ROOT / ".factory" / "telegram")
    status = gateway.get_status()

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Telegram Gateway Status:")
        print(f"  Configured: {status['configured']}")
        print(f"  Authorized Users: {status['authorized_user_count']}")
        print(f"  Last Offset: {status['last_offset']}")
        print(f"  Processed Updates: {status['processed_updates']}")
        print(f"  Pending Outbox: {status['pending_outbox_notifications']}")

    return 0


def cmd_telegram_send(args: argparse.Namespace) -> int:
    """Send a notification message via Telegram Gateway."""
    config = _load_telegram_config(args.config)
    gateway = TelegramGateway(config=config, state_dir=REPO_ROOT / ".factory" / "telegram")
    success = gateway.send_message(chat_id=args.chat_id, text=args.text)

    output = {"chat_id": args.chat_id, "sent": success, "queued": not success}
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        if success:
            print(f"Message delivered to chat {args.chat_id}")
        else:
            print(f"Message queued in local outbox (chat {args.chat_id})")

    return 0


def cmd_n8n_preflight(args: argparse.Namespace) -> int:
    """Run preflight probe against an n8n URL."""
    probe = N8nProbe()
    report = probe.probe(target_url=args.url)

    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
    else:
        print(f"n8n Preflight for: {report.url}")
        print(f"  Generic Placeholder: {report.is_generic_placeholder}")
        print(f"  Operational: {report.operational}")
        if report.version:
            print(f"  Version: {report.version}")
        if report.error:
            print(f"  Error: {report.error}")

    return 0 if report.operational else (1 if not report.is_generic_placeholder else 2)


def cmd_n8n_generate_compose(args: argparse.Namespace) -> int:
    """Generate Docker Compose YAML for n8n Community."""
    yaml_content = N8nManifestGenerator.generate_docker_compose(
        port=args.port,
        webhook_url=args.webhook_url,
    )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_content, encoding="utf-8")
        if args.json:
            print(json.dumps({"status": "generated", "file": str(out_path.resolve())}, indent=2))
        else:
            print(f"Manifest written to {out_path}")
    else:
        print(yaml_content)

    return 0


def cmd_n8n_export_workflow(args: argparse.Namespace) -> int:
    """Export and sanitize an n8n workflow file."""
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input file {in_path} not found", file=sys.stderr)
        return 2

    raw_data = json.loads(in_path.read_text(encoding="utf-8"))
    out_path = Path(args.output) if args.output else None
    sanitized = N8nWorkflowManager.export_workflow(raw_data, output_path=out_path)

    if args.json:
        print(json.dumps(sanitized, indent=2))
    else:
        if out_path:
            print(f"Sanitized workflow exported to {out_path}")
        else:
            print("Workflow sanitized successfully.")

    return 0


def cmd_n8n_import_workflow(args: argparse.Namespace) -> int:
    """Validate an n8n workflow file for Community edition."""
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input file {in_path} not found", file=sys.stderr)
        return 2

    raw_data = json.loads(in_path.read_text(encoding="utf-8"))
    valid = N8nWorkflowManager.import_workflow(raw_data)

    res = {"valid": valid, "file": str(in_path.resolve())}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Workflow {in_path.name}: {'VALID' if valid else 'INVALID'}")

    return 0 if valid else 1


def cmd_n8n_workflows(args: argparse.Namespace) -> int:
    """List workflows on n8n instance via API."""
    cfg = load_n8n_config()
    if args.url:
        cfg.base_url = args.url
    if args.api_key:
        cfg.api_key = args.api_key

    client = N8nApiClient(config=cfg)
    res = client.list_workflows(limit=args.limit)
    if args.json:
        print(json.dumps(res.model_dump(), indent=2))
    else:
        if res.success:
            wfs = (res.data or {}).get("data", []) if isinstance(res.data, dict) else []
            print(f"n8n Workflows ({len(wfs)} found on {cfg.base_url}):")
            for wf in wfs:
                active_str = "ACTIVE" if wf.get("active") else "INACTIVE"
                print(f"  [{active_str}] {wf.get('id')}: {wf.get('name')}")
        else:
            print(f"Error listing workflows: {res.error}", file=sys.stderr)
    return 0 if res.success else 1


def cmd_n8n_sync(args: argparse.Namespace) -> int:
    """Sync workflows from directory or file to n8n instance."""
    cfg = load_n8n_config()
    if args.url:
        cfg.base_url = args.url
    if args.api_key:
        cfg.api_key = args.api_key

    client = N8nApiClient(config=cfg)
    target_path = Path(args.path or (REPO_ROOT / ".factory" / "n8n" / "workflows"))

    if target_path.is_file():
        res = client.sync_workflow_file(target_path, activate=not args.no_activate)
        results = {target_path.name: res}
    else:
        results = client.sync_all_workflows(target_path, activate=not args.no_activate)

    success_all = all(r.success for r in results.values()) if results else False
    if args.json:
        out = {k: v.model_dump() for k, v in results.items()}
        print(json.dumps({"success": success_all, "results": out}, indent=2))
    else:
        print(f"Workflow Sync to {cfg.base_url} ({len(results)} items):")
        for fname, r in results.items():
            st = "OK" if r.success else f"FAIL ({r.error})"
            print(f"  {fname}: {st}")
    return 0 if success_all else 1


def cmd_n8n_trigger(args: argparse.Namespace) -> int:
    """Trigger an n8n webhook workflow."""
    cfg = load_n8n_config()
    if args.url:
        cfg.base_url = args.url
    if args.webhook_url:
        cfg.webhook_url = args.webhook_url
    if args.api_key:
        cfg.api_key = args.api_key

    client = N8nApiClient(config=cfg)
    try:
        if args.payload.startswith("@") or Path(args.payload).exists():
            p_file = Path(args.payload[1:] if args.payload.startswith("@") else args.payload)
            payload_data = json.loads(p_file.read_text(encoding="utf-8"))
        else:
            payload_data = json.loads(args.payload)
    except Exception as exc:
        err = {"error": f"Failed to parse payload: {exc}"}
        if args.json:
            print(json.dumps(err, indent=2))
        else:
            print(err["error"], file=sys.stderr)
        return 2

    res = client.trigger_webhook(args.path, payload_data, method=args.method, timeout=args.timeout)
    if args.json:
        print(json.dumps(res.model_dump(), indent=2))
    else:
        if res.success:
            print(f"Webhook triggered successfully: {res.status_code}")
            if res.data:
                print(f"Response: {res.data}")
        else:
            print(f"Webhook trigger failed: {res.error}", file=sys.stderr)
    return 0 if res.success else 1


def build_parser() -> argparse.ArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    common_parser.add_argument("--config", type=Path, default=None, help="Path to Telegram config JSON")

    parser = argparse.ArgumentParser(
        description="Headless Telegram Gateway and n8n Community CLI (HF-14)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # telegram-process-update
    p_update = subparsers.add_parser("telegram-process-update", help="Process raw Telegram update JSON", parents=[common_parser])
    p_update.add_argument("--payload", required=True, help="Update JSON string or @filepath")
    p_update.add_argument("--user-id", type=int, default=None, help="Authorize user ID inline")
    p_update.set_defaults(func=cmd_telegram_process_update)

    # telegram-status
    p_status = subparsers.add_parser("telegram-status", help="Get gateway status", parents=[common_parser])
    p_status.set_defaults(func=cmd_telegram_status)

    # telegram-send
    p_send = subparsers.add_parser("telegram-send", help="Send notification", parents=[common_parser])
    p_send.add_argument("--chat-id", type=int, required=True, help="Target chat ID")
    p_send.add_argument("--text", required=True, help="Message text")
    p_send.set_defaults(func=cmd_telegram_send)

    # n8n-preflight
    p_preflight = subparsers.add_parser("n8n-preflight", help="Probe an n8n endpoint", parents=[common_parser])
    p_preflight.add_argument("--url", default="https://n8n.io", help="n8n URL to probe")
    p_preflight.set_defaults(func=cmd_n8n_preflight)

    # n8n-generate-compose
    p_compose = subparsers.add_parser("n8n-generate-compose", help="Generate docker-compose.yml", parents=[common_parser])
    p_compose.add_argument("--output", type=Path, default=None, help="Output file path")
    p_compose.add_argument("--port", type=int, default=5678, help="Host port")
    p_compose.add_argument("--webhook-url", default="http://localhost:5678", help="Webhook URL")
    p_compose.set_defaults(func=cmd_n8n_generate_compose)

    # n8n-export-workflow
    p_export = subparsers.add_parser("n8n-export-workflow", help="Sanitize and export workflow", parents=[common_parser])
    p_export.add_argument("--input", required=True, help="Path to input workflow JSON")
    p_export.add_argument("--output", default=None, help="Path to output sanitized JSON")
    p_export.set_defaults(func=cmd_n8n_export_workflow)

    # n8n-import-workflow
    p_import = subparsers.add_parser("n8n-import-workflow", help="Validate workflow JSON", parents=[common_parser])
    p_import.add_argument("--input", required=True, help="Path to input workflow JSON")
    p_import.set_defaults(func=cmd_n8n_import_workflow)

    # n8n-workflows
    p_wfs = subparsers.add_parser("n8n-workflows", help="List remote n8n workflows", parents=[common_parser])
    p_wfs.add_argument("--url", default=None, help="n8n instance base URL")
    p_wfs.add_argument("--api-key", default=None, help="n8n API Key")
    p_wfs.add_argument("--limit", type=int, default=50, help="Maximum items to return")
    p_wfs.set_defaults(func=cmd_n8n_workflows)

    # n8n-sync
    p_sync = subparsers.add_parser("n8n-sync", help="Synchronize workflows to n8n instance", parents=[common_parser])
    p_sync.add_argument("--path", default=None, help="Path to directory or single workflow JSON")
    p_sync.add_argument("--url", default=None, help="n8n instance base URL")
    p_sync.add_argument("--api-key", default=None, help="n8n API Key")
    p_sync.add_argument("--no-activate", action="store_true", help="Do not activate workflows after upload")
    p_sync.set_defaults(func=cmd_n8n_sync)

    # n8n-trigger
    p_trig = subparsers.add_parser("n8n-trigger", help="Trigger an n8n webhook workflow", parents=[common_parser])
    p_trig.add_argument("--path", required=True, help="Webhook path or full URL (e.g. webhook/darkfac-alerts)")
    p_trig.add_argument("--payload", default="{}", help="JSON payload string or @filepath")
    p_trig.add_argument("--url", default=None, help="n8n instance base URL")
    p_trig.add_argument("--webhook-url", default=None, help="Webhook base URL override")
    p_trig.add_argument("--api-key", default=None, help="n8n API Key")
    p_trig.add_argument("--method", default="POST", help="HTTP Method")
    p_trig.add_argument("--timeout", type=float, default=15.0, help="Request timeout")
    p_trig.set_defaults(func=cmd_n8n_trigger)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
