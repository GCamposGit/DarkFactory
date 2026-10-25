from __future__ import annotations

import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.governance import authorization as auth

OWNER_KEY = Ed25519PrivateKey.generate()

PROTECTED = "core/orchestrator/guard.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet", "-b", "main")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    pubkey = tmp_path / auth.OWNER_PUBKEY_PATH
    pubkey.parent.mkdir(parents=True)
    raw = OWNER_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pubkey.write_text(base64.b64encode(raw).decode("ascii") + "\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "seed")
    _git(tmp_path, "checkout", "--quiet", "-b", "cand")
    target = tmp_path / PROTECTED
    target.parent.mkdir(parents=True)
    target.write_text("changed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "cand")
    return tmp_path


def _publish(repo: Path, *, signed: bool = True, **overrides: object) -> None:
    """Commit an owner-signed record on the trusted base (main), then return to cand."""
    record: dict[str, object] = {
        "id": "AUTH-1",
        "ticket_id": "USR-228",
        "approver": "owner",
        "paths": [PROTECTED],
        "candidate_kind": "tree",
        "candidate": _git(repo, "rev-parse", "HEAD^{tree}"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    record.update(overrides)
    if signed:
        record["signature"] = base64.b64encode(OWNER_KEY.sign(auth.canonical_payload(record))).decode("ascii")
    _git(repo, "checkout", "--quiet", "main")
    path = repo / auth.AUTHORIZATIONS_DIR / "AUTH-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "record")
    _git(repo, "checkout", "--quiet", "cand")


def _verify(repo: Path, paths: list[str] | None = None) -> auth.AuthorizationResult:
    return auth.verify_authorization(
        paths or [PROTECTED], repository=repo, trusted_base="main"
    )


def test_valid_tree_record_on_base_authorizes(repo: Path) -> None:
    _publish(repo)
    assert _verify(repo).ok


def test_valid_commit_record_authorizes(repo: Path) -> None:
    _publish(repo, candidate_kind="commit", candidate=_git(repo, "rev-parse", "HEAD"))
    assert _verify(repo).ok


def test_missing_record_blocks(repo: Path) -> None:
    assert not _verify(repo).ok


def test_unsigned_record_blocks(repo: Path) -> None:
    _publish(repo, signed=False)
    assert not _verify(repo).ok


def test_record_only_in_candidate_blocks(repo: Path) -> None:
    record = repo / auth.AUTHORIZATIONS_DIR / "SELF.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps(
            {
                "id": "SELF",
                "ticket_id": "USR-228",
                "approver": "owner",
                "paths": [PROTECTED],
                "candidate_kind": "commit",
                "candidate": "0" * 40,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "self-issued")
    assert not _verify(repo).ok


def test_expired_blocks(repo: Path) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    _publish(repo, expires_at=past)
    result = _verify(repo)
    assert not result.ok and "expired" in result.reason


def test_other_candidate_blocks(repo: Path) -> None:
    _publish(repo, candidate="f" * 40)
    result = _verify(repo)
    assert not result.ok and "candidate mismatch" in result.reason


def test_other_paths_block(repo: Path) -> None:
    _publish(repo, paths=["AGENTS.md"])
    assert not _verify(repo).ok


def test_partial_paths_block(repo: Path) -> None:
    _publish(repo)
    assert not _verify(repo, [PROTECTED, "AGENTS.md"]).ok


def test_glob_paths_rejected(repo: Path) -> None:
    _publish(repo, paths=["core/orchestrator/*"])
    assert not _verify(repo).ok


def test_non_owner_approver_blocks(repo: Path) -> None:
    _publish(repo, approver="build-agent")
    assert not _verify(repo).ok


def test_malformed_and_extra_fields_block(repo: Path) -> None:
    _publish(repo, unexpected=True)
    assert not _verify(repo).ok


def test_dirty_worktree_blocks(repo: Path) -> None:
    _publish(repo)
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    assert not _verify(repo).ok


def test_unreadable_base_blocks(repo: Path) -> None:
    result = auth.verify_authorization(
        [PROTECTED], repository=repo, trusted_base="origin/does-not-exist"
    )
    assert not result.ok


def test_env_flag_alone_is_not_proof(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", "1")
    assert not _verify(repo).ok
