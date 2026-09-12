"""Headless CLI interface for Dark Factory Notification and Token Quota Alert System."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

# Ensure robust UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.notifications.models import AlertCategory, AlertSeverity
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore
from core.notifications.token_watcher import TokenQuotaWatcher


def cmd_check_quotas(args: argparse.Namespace) -> int:
    """Evaluates token quotas across all accounts and triggers alerts."""
    watcher = TokenQuotaWatcher()
    emitted = watcher.check_all_quotas(force=args.force)

    if args.json:
        print(json.dumps([e.model_dump(mode="json") for e in emitted], indent=2))
    else:
        print(f"=== Quota Check Completed ({len(emitted)} alert(s) emitted) ===")
        for e in emitted:
            icon = "🚨" if e.severity == AlertSeverity.CRITICAL else "⚠️"
            print(f"  {icon} [{e.severity.value.upper()}] {e.title}: {e.provider_id} ({e.remaining_percent}%)")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Lists recent notifications from the store."""
    store = NotificationStore()
    sev = AlertSeverity(args.severity) if args.severity else None
    events = store.list_notifications(limit=args.limit, severity=sev, unread_only=args.unread)

    if args.json:
        print(json.dumps([e.model_dump(mode="json") for e in events], indent=2))
    else:
        print(f"=== Recent Notifications ({len(events)}) ===")
        for e in events:
            ack_mark = "[LIDO]" if e.acknowledged else "[NOVO]"
            print(f"  {ack_mark} [{e.severity.value.upper()}] {e.title} - {e.timestamp.isoformat()}")
            print(f"         {e.message}")

    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    """Dispatches a synthetic test alert to verify Telegram and DarkHub delivery."""
    svc = NotificationService()
    event = svc.notify(
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.WARNING,
        title="Alerta de Teste Operacional",
        message="Mensagem de teste do sistema de notificações e monitoramento de tokens.",
        provider_id="openai-test",
        remaining_percent=24.5,
        force=True,
    )

    if args.json:
        print(json.dumps(event.model_dump(mode="json") if event else {}, indent=2))
    else:
        print(f"✅ Test Alert Emitted: {event.notification_id if event else 'Suppressed'}")
        if event:
            print(f"  Delivered Channels: {event.delivered_channels}")

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dark Factory Notifications and Quota Alert CLI"
    )
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="Format output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # check-quotas
    check_parser = subparsers.add_parser("check-quotas", parents=[json_parent], help="Check token quotas")
    check_parser.add_argument("--force", action="store_true", help="Force cache refresh")

    # list
    list_parser = subparsers.add_parser("list", parents=[json_parent], help="List notifications")
    list_parser.add_argument("--limit", type=int, default=20, help="Max notifications to retrieve")
    list_parser.add_argument("--severity", choices=["info", "warning", "critical"], default=None)
    list_parser.add_argument("--unread", action="store_true", help="Only unacknowledged notifications")

    # test-alert
    subparsers.add_parser("test-alert", parents=[json_parent], help="Emit a test alert")

    args = parser.parse_args(argv)

    dispatch = {
        "check-quotas": cmd_check_quotas,
        "list": cmd_list,
        "test-alert": cmd_test_alert,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
