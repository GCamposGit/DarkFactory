"""Authenticated Encryption (AES-256-GCM) for Dark Factory Backups (INFRA-08).

Governed by:
- INFRA-08: Automação de Backups 3-Camadas (R2 + On-Premise)
- Invariant: Fail-closed on tamper or incorrect key; authenticated encryption with AEAD.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC_HEADER: Final[bytes] = b"DFENC01\0"  # 8 bytes magic header
SALT_SIZE: Final[int] = 16  # 16 bytes salt for PBKDF2
NONCE_SIZE: Final[int] = 12  # 12 bytes recommended for AES-GCM
PBKDF2_ITERATIONS: Final[int] = 100_000


class CryptoError(Exception):
    """Base error for crypto operations."""


class DecryptionError(CryptoError):
    """Raised when decryption fails due to corrupted data, tampering, or wrong key."""


class CorruptedArchiveError(CryptoError):
    """Raised when file header or magic bytes do not match expected format."""


def _derive_key(passphrase_or_key: str | bytes, salt: bytes) -> bytes:
    """Derives a 256-bit AES key using PBKDF2-HMAC-SHA256."""
    raw = (
        passphrase_or_key.encode("utf-8")
        if isinstance(passphrase_or_key, str)
        else passphrase_or_key
    )
    if not raw:
        raise ValueError("Encryption passphrase or key cannot be empty.")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,  # 256 bits
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(raw)


def encrypt_bytes(data: bytes, passphrase_or_key: str | bytes) -> bytes:
    """Encrypts plaintext bytes using AES-256-GCM with PBKDF2 key derivation."""
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(passphrase_or_key, salt)
    nonce = os.urandom(NONCE_SIZE)

    aesgcm = AESGCM(key)
    # Associated authenticated data includes MAGIC_HEADER + salt + nonce
    aad = MAGIC_HEADER + salt + nonce
    ciphertext = aesgcm.encrypt(nonce, data, associated_data=aad)

    return aad + ciphertext


def decrypt_bytes(encrypted_data: bytes, passphrase_or_key: str | bytes) -> bytes:
    """Decrypts ciphertext bytes, verifying magic header and GCM authentication tag."""
    min_len = len(MAGIC_HEADER) + SALT_SIZE + NONCE_SIZE + 16  # 16 bytes GCM tag
    if len(encrypted_data) < min_len:
        raise CorruptedArchiveError(
            f"Encrypted payload too short ({len(encrypted_data)} bytes, min {min_len} required)."
        )

    header_len = len(MAGIC_HEADER)
    magic = encrypted_data[:header_len]
    if magic != MAGIC_HEADER:
        raise CorruptedArchiveError(
            f"Invalid archive header magic: expected {MAGIC_HEADER!r}, got {magic!r}"
        )

    salt_offset = header_len + SALT_SIZE
    salt = encrypted_data[header_len:salt_offset]

    nonce_offset = salt_offset + NONCE_SIZE
    nonce = encrypted_data[salt_offset:nonce_offset]

    aad = encrypted_data[:nonce_offset]
    ciphertext = encrypted_data[nonce_offset:]

    key = _derive_key(passphrase_or_key, salt)
    aesgcm = AESGCM(key)

    try:
        return aesgcm.decrypt(nonce, ciphertext, associated_data=aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "Authentication tag mismatch: data corrupted, tampered, or wrong encryption key."
        ) from exc


def encrypt_file(
    source_path: Path | str,
    dest_path: Path | str,
    passphrase_or_key: str | bytes,
) -> Path:
    """Encrypts a file from source_path to dest_path."""
    src = Path(source_path).resolve()
    dst = Path(dest_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Source file not found: {src}")

    plaintext = src.read_bytes()
    encrypted = encrypt_bytes(plaintext, passphrase_or_key)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encrypted)
    return dst


def decrypt_file(
    source_path: Path | str,
    dest_path: Path | str,
    passphrase_or_key: str | bytes,
) -> Path:
    """Decrypts an encrypted file from source_path to dest_path."""
    src = Path(source_path).resolve()
    dst = Path(dest_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Encrypted source file not found: {src}")

    encrypted = src.read_bytes()
    plaintext = decrypt_bytes(encrypted, passphrase_or_key)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(plaintext)
    return dst


__all__ = [
    "CorruptedArchiveError",
    "CryptoError",
    "DecryptionError",
    "decrypt_bytes",
    "decrypt_file",
    "encrypt_bytes",
    "encrypt_file",
]
