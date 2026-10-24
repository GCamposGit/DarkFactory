"""Bootstrap do cofre SOPS+age (USR-343 S5).

  python C:\\dev\\DarkFac\\scripts\\bootstrap_secrets.py            # cadastra so os faltantes
  python C:\\dev\\DarkFac\\scripts\\bootstrap_secrets.py --check    # lista faltantes; sai 1 se houver
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.infra import secrets_vault as vault  # noqa: E402


def missing_keys(path: Path = vault.VAULT_FILE) -> list[str]:
    have = vault.decrypt(path)
    return [k for k in vault.required_keys() if not have.get(k)]


def _encrypt(data: dict[str, str], path: Path) -> None:
    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops nao encontrado no PATH (veja docs/manual/segredos.md)")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temp file na mesma pasta (com sufixo .enc.yaml) para casar com creation_rules do .sops.yaml.
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="tmp-", suffix=".enc.yaml", delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(json.dumps(data))
        proc = subprocess.run(
            [sops, "-e", "--input-type", "json", "--output-type", "yaml", str(tmp_path)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"sops -e falhou (codigo {proc.returncode}); confira .sops.yaml")
    path.write_text(proc.stdout, encoding="utf-8")


def add_missing(path: Path = vault.VAULT_FILE) -> list[str]:
    data = vault.decrypt(path)
    added: list[str] = []
    for key in vault.required_keys():
        if data.get(key):
            continue
        value = getpass.getpass(f"{key}: ")
        if value:
            data[key] = value
            added.append(key)
    if added:
        _encrypt(data, path)
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="lista chaves faltantes")
    args = ap.parse_args(argv)
    if args.check:
        miss = missing_keys()
        for key in miss:
            print(f"FALTA {key}")
        return 1 if miss else 0
    try:
        added = add_missing()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"cadastradas: {', '.join(added) if added else 'nenhuma'}")
    return 1 if missing_keys() else 0


if __name__ == "__main__":
    sys.exit(main())
