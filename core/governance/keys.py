"""Chaves Ed25519 do owner para autorizacao de governanca (USR-343 S1).

Uso (PowerShell):
  python -m core.governance.keys init                   # gera o par; privada fora do repo
  python -m core.governance.keys sign <registro.json>   # assina um registro

A chave privada nunca entra no repositorio: fica em ``~/.darkfac/`` (ou no
caminho de ``--private``). Apenas a publica vai para
``.factory/governance/owner_pubkey`` (commit do owner).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.governance.authorization import OWNER_PUBKEY_PATH, canonical_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE = Path.home() / ".darkfac" / "owner_ed25519.pem"


def init_keys(private_path: Path, public_path: Path) -> None:
    if private_path.exists():
        raise FileExistsError(f"private key already exists: {private_path}")
    key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(base64.b64encode(raw).decode("ascii") + "\n", encoding="utf-8")


def sign_record(record_path: Path, private_path: Path) -> None:
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["signature"] = base64.b64encode(key.sign(canonical_payload(record))).decode("ascii")
    record_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.governance.keys")
    parser.add_argument("--private", type=Path, default=DEFAULT_PRIVATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sign = sub.add_parser("sign")
    sign.add_argument("record", type=Path)
    args = parser.parse_args(argv)
    public = REPO_ROOT / OWNER_PUBKEY_PATH
    try:
        if args.command == "init":
            init_keys(args.private, public)
            print(f"private key: {args.private}\npublic key: {public} (commit this)")
        else:
            sign_record(args.record, args.private)
            print(f"signed: {args.record}")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
