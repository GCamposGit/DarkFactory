#!/usr/bin/env python3
"""Diagnostico e replay sanitizado de um alerta Telegram (USR-536).

O recibo leva notification_id, papel do bot, message_id e timestamp.
Token, chat_id, titulo e mensagem ficam de fora. Sem token ou sem destino
a execucao e recusada.

PowerShell:

    python C:\\dev\\DarkFac\\scripts\\telegram_delivery_check.py --notification-id notif_3e46d90ee946
    python C:\\dev\\DarkFac\\scripts\\telegram_delivery_check.py --notification-id notif_3e46d90ee946 --replay
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.notifications.telegram_delivery import main  # noqa: E402

__all__ = ["REPO_ROOT", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
