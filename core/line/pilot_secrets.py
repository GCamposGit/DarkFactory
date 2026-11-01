"""Prontidao de segredos de piloto (USR-279 T4): piloto falha fechado sem credencial.

`core.infra.secrets_vault` ja le o cofre SOPS+age e expoe `required_keys()` (lista unica em
`.factory/config/secrets_required.json`). Este modulo nao duplica essa lista: `mansaodolago_readiness`
filtra `required_keys()` pelo prefixo `MANSAODOLAGO_` para saber quais chaves o piloto precisa, e
devolve so presente/ausente por chave -- nunca o valor, em log, json ou saida de CLI.

`require_pilot_secret` e o unico jeito do codigo do piloto (ainda nao implementado; chega em ticket
futuro de M1) obter um segredo: levanta `PilotSecretMissingError` quando a chave nao esta no cofre,
em vez de assumir um valor ou seguir com mock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel

from core.infra import secrets_vault

MANSAODOLAGO_PREFIX = "MANSAODOLAGO_"
MANSAODOLAGO_SEARCH_CONSOLE_KEY = "MANSAODOLAGO_SEARCH_CONSOLE_CREDENTIALS_JSON"
MANSAODOLAGO_INSTAGRAM_GRAPH_KEY = "MANSAODOLAGO_INSTAGRAM_GRAPH_ACCESS_TOKEN"
MANSAODOLAGO_GOOGLE_ADS_KEY = "MANSAODOLAGO_GOOGLE_ADS_REFRESH_TOKEN"


class PilotSecretMissingError(RuntimeError):
    """Levantada quando o codigo do piloto pede uma chave ausente no cofre (fail-closed)."""

    def __init__(self, key: str):
        super().__init__(f"segredo do piloto ausente no cofre: {key}")
        self.key = key


class PilotSecretStatus(BaseModel):
    key: str
    present: bool


class PilotReadinessReport(BaseModel):
    project_id: str
    keys: list[PilotSecretStatus]

    @property
    def ready(self) -> bool:
        return all(k.present for k in self.keys)

    @property
    def missing_keys(self) -> list[str]:
        return [k.key for k in self.keys if not k.present]


def pilot_readiness(
    project_id: str, keys: tuple[str, ...], *, vault: Path = secrets_vault.VAULT_FILE
) -> PilotReadinessReport:
    """So presente/ausente por chave; nunca le nem devolve o valor do segredo."""
    have = secrets_vault.decrypt(vault)
    return PilotReadinessReport(
        project_id=project_id,
        keys=[PilotSecretStatus(key=k, present=bool(have.get(k))) for k in keys],
    )


def mansaodolago_readiness(*, vault: Path = secrets_vault.VAULT_FILE) -> PilotReadinessReport:
    """Chaves do piloto = `required_keys()` filtrada pelo prefixo `MANSAODOLAGO_` (fonte unica)."""
    keys = tuple(k for k in secrets_vault.required_keys() if k.startswith(MANSAODOLAGO_PREFIX))
    return pilot_readiness("mansaodolago", keys, vault=vault)


def require_pilot_secret(key: str, *, vault: Path = secrets_vault.VAULT_FILE) -> str:
    """Unico jeito do codigo do piloto obter um segredo; falha fechado sem mock."""
    value = secrets_vault.decrypt(vault).get(key)
    if not value:
        raise PilotSecretMissingError(key)
    return value


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="DarkFac: prontidao de segredos do piloto mansaodolago.com (USR-279 T4)"
    )
    parser.parse_args(argv)

    report = mansaodolago_readiness()
    for status in report.keys:
        print(f"{'presente' if status.present else 'ausente'}  {status.key}")
    if report.ready:
        print("piloto mansaodolago: pronto (todas as chaves cadastradas no cofre)")
        return 0
    print(f"piloto mansaodolago: nao pronto -- falta(m): {', '.join(report.missing_keys)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
