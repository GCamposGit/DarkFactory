"""USR-343 S3: aprovacao por toque no Telegram para PRs protegidos."""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.governance import authorization as auth
from core.governance import publication as pub

PROTECTED = "core/orchestrator/guard.py"
OWNER = 4242


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class FakePort:
    def __init__(self, repo: Path, pubkey_b64: str) -> None:
        self.repo, self.pubkey = repo, pubkey_b64
        self.merged: list[tuple[int, str]] = []
        self.green = True

    def get_pull_request(self, number: int) -> pub.PullRequestInfo:
        return pub.PullRequestInfo(number, _git(self.repo, "rev-parse", "cand"), (PROTECTED,), "USR-343")

    def diff_summary(self, number: int) -> str:
        return f"{PROTECTED} (+1/-0)"

    def commit_to_main(self, path: str, content: str, message: str) -> str:
        _git(self.repo, "checkout", "--quiet", "main")
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "-m", message)
        sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "--quiet", "cand")
        return sha

    def checks_green(self, sha: str) -> bool:
        return self.green

    def merge(self, number: int, expected_head_sha: str) -> bool:
        self.merged.append((number, expected_head_sha))
        return True


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def port(tmp_path: Path, key: Ed25519PrivateKey) -> FakePort:
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pubkey = base64.b64encode(raw).decode("ascii")
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@example.invalid")
    (repo / ".factory/governance").mkdir(parents=True)
    (repo / auth.OWNER_PUBKEY_PATH).write_text(pubkey + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "seed")
    _git(repo, "checkout", "--quiet", "-b", "cand")
    (repo / PROTECTED).parent.mkdir(parents=True)
    (repo / PROTECTED).write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "cand")
    return FakePort(repo, pubkey)


def _tap(port: FakePort, key: Ed25519PrivateKey, tmp_path: Path, sender: int, action: str = "a", sha: str | None = None):
    head = sha if sha is not None else port.get_pull_request(1).head_sha[:12]
    return pub.handle_callback(
        {"from": {"id": sender}, "data": f"pp:{action}:1:{head}"},
        owner_chat_id=OWNER,
        port=port,
        key_loader=lambda: key,
        pending_file=tmp_path / "pending.json",
        audit_log=tmp_path / "audit.jsonl",
        now=datetime.now(timezone.utc),
    )


def test_non_owner_callback_is_rejected(port: FakePort, key: Ed25519PrivateKey, tmp_path: Path) -> None:
    result = _tap(port, key, tmp_path, sender=999)
    assert result == {"ok": False, "reason": "not owner"}
    assert not (tmp_path / "pending.json").exists() and port.merged == []
    assert not (port.repo / pub.AUTHORIZATIONS_DIR).exists() or _git(port.repo, "branch", "--show-current") == "cand"


def test_owner_tap_signs_record_that_guard_accepts_and_merge_waits_for_checks(
    port: FakePort, key: Ed25519PrivateKey, tmp_path: Path
) -> None:
    result = _tap(port, key, tmp_path, sender=OWNER)
    assert result["ok"] and result["record"]["paths"] == [PROTECTED]
    assert auth.verify_authorization([PROTECTED], repository=port.repo, trusted_base="main").ok
    pending, log = tmp_path / "pending.json", tmp_path / "audit.jsonl"
    port.green = False
    assert len(pub.sweep_pending(port, pending, log)) == 1 and port.merged == []
    port.green = True
    sha = port.get_pull_request(1).head_sha
    assert pub.sweep_pending(port, pending, log) == [] and port.merged == [(1, sha)]


def test_tampered_record_fails(port: FakePort, key: Ed25519PrivateKey, tmp_path: Path) -> None:
    result = _tap(port, key, tmp_path, sender=OWNER)
    path = port.repo / pub.AUTHORIZATIONS_DIR / f"{result['record']['id']}.json"
    _git(port.repo, "checkout", "--quiet", "main")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["paths"] = [PROTECTED, "AGENTS.md"]
    path.write_text(json.dumps(record), encoding="utf-8")
    _git(port.repo, "commit", "--quiet", "-am", "tamper")
    _git(port.repo, "checkout", "--quiet", "cand")
    assert not auth.verify_authorization([PROTECTED], repository=port.repo, trusted_base="main").ok


def test_head_sha_change_invalidates(port: FakePort, key: Ed25519PrivateKey, tmp_path: Path) -> None:
    stale = port.get_pull_request(1).head_sha[:12]
    assert _tap(port, key, tmp_path, sender=OWNER, sha="0" * 12)["ok"] is False
    _tap(port, key, tmp_path, sender=OWNER)
    (port.repo / PROTECTED).write_text("changed again\n", encoding="utf-8")
    _git(port.repo, "commit", "--quiet", "-am", "more")
    assert not auth.verify_authorization([PROTECTED], repository=port.repo, trusted_base="main").ok
    assert pub.sweep_pending(port, tmp_path / "pending.json", tmp_path / "audit.jsonl") == []
    assert port.merged == [] and stale != port.get_pull_request(1).head_sha[:12]


def test_notify_has_buttons_and_signing_key_is_denied_to_agents(port: FakePort) -> None:
    sent: list[tuple[int, str, list]] = []
    assert pub.notify_owner(port, 1, lambda c, t, b: sent.append((c, t, b)), OWNER)
    chat, text, buttons = sent[0]
    assert chat == OWNER and PROTECTED in text and "pp:a:1:" in buttons[0][0]["callback_data"]
    from core.line.agent_env import build_agent_env

    assert "OWNER_SIGNING_KEY" not in build_agent_env({"OWNER_SIGNING_KEY": "x", "PATH": "p"})
