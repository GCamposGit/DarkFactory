"""Cofre SOPS+age (USR-343 S5): le secrets/factory.enc.yaml via `sops -d`.

No-op quando o `sops` ou o arquivo nao existem. Nunca usado por agentes de codigo.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
VAULT_FILE = REPO_ROOT / "secrets" / "factory.enc.yaml"
REQUIRED_FILE = REPO_ROOT / ".factory" / "config" / "secrets_required.json"


def required_keys(path: Path = REQUIRED_FILE) -> list[str]:
    try:
        return [str(k) for k in json.loads(path.read_text(encoding="utf-8")).get("required", [])]
    except (OSError, ValueError):
        return []


def decrypt(vault: Path = VAULT_FILE) -> dict[str, str]:
    """Retorna o cofre decifrado; {} se sops/arquivo ausentes ou falha."""
    sops = shutil.which("sops")
    if not sops or not vault.is_file():
        return {}
    try:
        proc = subprocess.run(
            [sops, "-d", "--output-type", "json", str(vault)],
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("sops indisponivel: %s", type(exc).__name__)
        return {}
    if proc.returncode != 0:
        logger.warning("sops -d falhou (codigo %s)", proc.returncode)
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def load_into_env(vault: Path = VAULT_FILE, *, override: bool = False) -> list[str]:
    """Exporta o cofre para os.environ; retorna os nomes carregados."""
    loaded: list[str] = []
    for key, value in decrypt(vault).items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
