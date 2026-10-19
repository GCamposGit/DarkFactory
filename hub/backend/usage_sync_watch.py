"""Vigia do loop DarkFac-Usage-Cloud-Sync, no lado do Hub (USR-235).

O host que sincroniza as cotas pode estar parado, sem chave ou recusado; por isso quem decide o
alerta e o Hub. O estado (ultimo sucesso, ultima recusa, causas ja notificadas) fica em um JSON no
volume de dados do Hub. A avaliacao acontece quando a fila de intervencoes e montada; a causa ativa
vira um item critico (idempotente por causa) e a primeira ocorrencia de cada causa dispara uma unica
mensagem no Telegram ops. Quando a sincronizacao volta, o item some e as notificacoes sao rearmadas.
Nenhum segredo e gravado ou enviado.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=5)

CAUSE_KEY_MISSING = "key_missing"
CAUSE_UNAUTHORIZED = "unauthorized_401"
CAUSE_STALE = "stale"

_CAUSE_TEXT: Dict[str, tuple[str, str]] = {
    CAUSE_KEY_MISSING: (
        "Sincronizacao de cotas recusada: Hub sem DARKFAC_TELEMETRY_KEY",
        "O Hub recebeu uma tentativa de POST /api/usage/sync mas nao tem DARKFAC_TELEMETRY_KEY configurada (HTTP 503).",
    ),
    CAUSE_UNAUTHORIZED: (
        "Sincronizacao de cotas recusada com 401",
        "POST /api/usage/sync foi recusado com 401: a chave do host difere da DARKFAC_TELEMETRY_KEY do Hub.",
    ),
    CAUSE_STALE: (
        "Sincronizacao de cotas parada ha mais de 5 minutos",
        "Nenhuma sincronizacao bem-sucedida em POST /api/usage/sync ha mais de 5 minutos (host parado ou tarefa DarkFac-Usage-Cloud-Sync falhando).",
    ),
}


@dataclass(frozen=True)
class UsageSyncAlert:
    cause: str
    title: str
    description: str
    since: str
    last_success_at: Optional[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _telegram_ops_notifier(text: str) -> bool:
    """Same ops-bot path as ``HubService.notify_pending_grill``: load_telegram_config + TelegramGateway."""
    from core.integrations.telegram import TelegramGateway, load_telegram_config

    cfg = load_telegram_config(role="ops")
    if not cfg.bot_token:
        return False
    gateway = TelegramGateway(config=cfg)
    sent = False
    for chat_id in list(cfg.authorized_chat_ids) or list(cfg.authorized_user_ids):
        if gateway.send_message(chat_id=chat_id, text=text):
            sent = True
    return sent


class UsageSyncWatch:
    def __init__(
        self,
        state_path: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        notifier: Callable[[str], bool] = _telegram_ops_notifier,
    ) -> None:
        self.state_path = Path(state_path)
        self.clock = clock
        self.notifier = notifier
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("usage sync watch: could not persist state (%s)", exc.__class__.__name__)

    # -- recording (called by /api/usage/sync) ------------------------
    def record_success(self) -> None:
        with self._lock:
            self._save({"last_success_at": self.clock().isoformat(), "notified": []})

    def record_refusal(self, cause: str) -> None:
        if cause not in (CAUSE_KEY_MISSING, CAUSE_UNAUTHORIZED):
            return
        with self._lock:
            state = self._load()
            now = self.clock().isoformat()
            state["last_refusal"] = {"cause": cause, "at": now}
            state.setdefault("first_seen_at", now)
            self._save(state)

    # -- evaluation (called when the priority queue is built) ----------
    def evaluate(self) -> Optional[UsageSyncAlert]:
        with self._lock:
            state = self._load()
            now = self.clock()
            last_success = _parse(state.get("last_success_at"))
            refusal = state.get("last_refusal") or {}
            refusal_at = _parse(refusal.get("at"))
            first_seen = _parse(state.get("first_seen_at"))

            cause: Optional[str] = None
            since: Optional[datetime] = None
            if refusal_at and (last_success is None or refusal_at > last_success) and refusal.get("cause") in _CAUSE_TEXT:
                cause, since = str(refusal["cause"]), refusal_at
            else:
                reference = last_success or first_seen
                if reference is not None and now - reference > STALE_AFTER:
                    cause, since = CAUSE_STALE, reference + STALE_AFTER

            if cause is None or since is None:
                return None

            title, description = _CAUSE_TEXT[cause]
            alert = UsageSyncAlert(
                cause=cause,
                title=title,
                description=description,
                since=since.isoformat(),
                last_success_at=last_success.isoformat() if last_success else None,
            )
            notified = list(state.get("notified") or [])
            if cause not in notified:
                state["notified"] = notified + [cause]
                self._save(state)
                self._notify(alert)
            return alert

    def _notify(self, alert: UsageSyncAlert) -> None:
        text = (
            "<b>DarkHub: sincronizacao de cotas degradada</b>\n"
            f"{alert.title}\n"
            f"Desde: <code>{alert.since}</code>\n"
            "Veja a fila de intervencoes prioritarias do DarkHub."
        )
        try:
            self.notifier(text)
        except Exception as exc:  # the queue must keep working
            logger.warning("usage sync watch: telegram notification failed (%s)", exc.__class__.__name__)
