"""Tests for `FtpMirrorDeploymentAdapter` (HF-27-07).

Uses an in-memory fake standing in for `ftplib.FTP` (`FakeFTP` below) so the
suite never opens a real socket or depends on a local FTP daemon, and passes
on both Windows and Linux CI runners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.deployment_adapter import (
    DeploymentStatus,
    FtpMirrorDeploymentAdapter,
    TargetConfig,
)


class FakeFTP:
    """In-memory stand-in for `ftplib.FTP`, backed by a shared dict "server"."""

    def __init__(self, fs: dict[str, bytes], dirs: set[str], *, support_symlink: bool = False) -> None:
        self.fs = fs
        self.dirs = dirs
        self.support_symlink = support_symlink
        self.quit_called = False

    def connect(self, host: str, port: int, timeout: float | None = None) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def sendcmd(self, cmd: str) -> str:
        if cmd.startswith("SITE SYMLINK") and not self.support_symlink:
            raise Exception("500 Command not understood")
        return "200 ok"

    def mkd(self, path: str) -> str:
        self.dirs.add(path)
        return path

    def storbinary(self, cmd: str, fh: Any) -> None:
        assert cmd.startswith("STOR ")
        path = cmd[len("STOR "):]
        self.fs[path] = fh.read()

    def retrbinary(self, cmd: str, callback: Any) -> None:
        assert cmd.startswith("RETR ")
        path = cmd[len("RETR "):]
        if path not in self.fs:
            raise Exception("550 No such file")
        callback(self.fs[path])

    def delete(self, path: str) -> None:
        self.fs.pop(path, None)

    def mlsd(self, path: str = ""):
        prefix = f"{path.rstrip('/')}/" if path else ""
        names: dict[str, str] = {}
        for full in self.fs:
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            if not rest:
                continue
            head = rest.split("/", 1)[0]
            names[head] = "dir" if "/" in rest else names.get(head, "file")
        for d in self.dirs:
            if d.startswith(prefix) and d != path.rstrip("/"):
                rest = d[len(prefix):]
                if rest and "/" not in rest:
                    names.setdefault(rest, "dir")
        return [(name, {"type": t}) for name, t in sorted(names.items())]

    def quit(self) -> None:
        self.quit_called = True


def _make_factory(fs: dict[str, bytes], dirs: set[str], *, support_symlink: bool = False):
    def factory(target_config: TargetConfig) -> FakeFTP:
        return FakeFTP(fs, dirs, support_symlink=support_symlink)

    return factory


def _target_config(dist_dir: Path, *, last_known_good_digest: str | None = None) -> TargetConfig:
    return TargetConfig(
        project_id="atrium",
        target_type="hostinger_ftp",
        metadata={"dist_dir": str(dist_dir), "remote_root": "public_html", "releases_dir": "releases"},
        last_known_good_digest=last_known_good_digest,
    )


def _artifact(sha: str) -> ArtifactRef:
    return ArtifactRef(artifact_id=sha, source_sha=sha, byte_digest=sha, byte_size=0)


def test_start_mirrors_dist_dir_and_writes_marker(tmp_path: Path) -> None:
    dist = tmp_path / "dist_v1"
    dist.mkdir()
    (dist / "index.html").write_text("v1", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("v1js", encoding="utf-8")

    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    adapter = FtpMirrorDeploymentAdapter(ftp_factory=_make_factory(fs, dirs))

    sha1 = "a" * 40
    target_config = _target_config(dist)
    op = adapter.start(_artifact(sha1), target_config)

    assert op.status == DeploymentStatus.SUCCEEDED
    assert fs["public_html/index.html"] == b"v1"
    assert fs["public_html/assets/app.js"] == b"v1js"
    assert fs[f"releases/{sha1}/index.html"] == b"v1"
    assert fs["public_html/.darkfac-release"] == sha1.encode("utf-8")
    assert op.details["previous_sha"] is None
    assert op.details["symlinked"] is False  # FakeFTP defaults to unsupported


def test_start_deletes_extraneous_files_and_tracks_previous_sha(tmp_path: Path) -> None:
    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    factory = _make_factory(fs, dirs)

    dist1 = tmp_path / "dist_v1"
    dist1.mkdir()
    (dist1 / "index.html").write_text("v1", encoding="utf-8")
    (dist1 / "old.html").write_text("stale", encoding="utf-8")

    sha1 = "a" * 40
    adapter1 = FtpMirrorDeploymentAdapter(ftp_factory=factory)
    adapter1.start(_artifact(sha1), _target_config(dist1))

    dist2 = tmp_path / "dist_v2"
    dist2.mkdir()
    (dist2 / "index.html").write_text("v2", encoding="utf-8")
    # old.html is intentionally absent from dist2: --delete semantics must
    # remove it from the web root on the next mirror.

    sha2 = "b" * 40
    adapter2 = FtpMirrorDeploymentAdapter(ftp_factory=factory)
    op2 = adapter2.start(_artifact(sha2), _target_config(dist2))

    assert op2.status == DeploymentStatus.SUCCEEDED
    assert op2.details["previous_sha"] == sha1
    assert fs["public_html/index.html"] == b"v2"
    assert "public_html/old.html" not in fs
    # the sha1 release directory is kept (never deleted) so rollback can reuse it
    assert fs[f"releases/{sha1}/old.html"] == b"stale"
    assert fs["public_html/.darkfac-release"] == sha2.encode("utf-8")


def test_installed_digest_reads_marker(tmp_path: Path) -> None:
    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    adapter = FtpMirrorDeploymentAdapter(ftp_factory=_make_factory(fs, dirs))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("v1", encoding="utf-8")

    sha1 = "a" * 40
    target_config = _target_config(dist)
    adapter.start(_artifact(sha1), target_config)

    assert adapter.installed_digest(target_config) == sha1


def test_reconcile_returns_terminal_status_recorded_by_start(tmp_path: Path) -> None:
    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    adapter = FtpMirrorDeploymentAdapter(ftp_factory=_make_factory(fs, dirs))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("v1", encoding="utf-8")

    op = adapter.start(_artifact("a" * 40), _target_config(dist))
    assert adapter.reconcile(op.operation_id) == DeploymentStatus.SUCCEEDED

    with pytest.raises(ValueError):
        adapter.reconcile("unknown-op")


def test_rollback_redeploys_previous_release_from_server(tmp_path: Path) -> None:
    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    factory = _make_factory(fs, dirs)

    dist1 = tmp_path / "dist_v1"
    dist1.mkdir()
    (dist1 / "index.html").write_text("v1", encoding="utf-8")

    sha1 = "a" * 40
    adapter = FtpMirrorDeploymentAdapter(ftp_factory=factory)
    adapter.start(_artifact(sha1), _target_config(dist1))

    dist2 = tmp_path / "dist_v2"
    dist2.mkdir()
    (dist2 / "index.html").write_text("v2-broken", encoding="utf-8")

    sha2 = "b" * 40
    target_config = _target_config(dist2, last_known_good_digest=sha1)
    adapter.start(_artifact(sha2), target_config)
    assert fs["public_html/index.html"] == b"v2-broken"

    result = adapter.rollback(target_config, failed_digest=sha2, reason="smoke failed")

    assert result.status == DeploymentStatus.ROLLED_BACK
    assert result.restored_digest == sha1
    assert fs["public_html/index.html"] == b"v1"
    assert fs["public_html/.darkfac-release"] == sha1.encode("utf-8")


def test_rollback_without_last_known_good_digest_fails(tmp_path: Path) -> None:
    fs: dict[str, bytes] = {}
    dirs: set[str] = set()
    adapter = FtpMirrorDeploymentAdapter(ftp_factory=_make_factory(fs, dirs))
    target_config = _target_config(tmp_path / "dist", last_known_good_digest=None)

    result = adapter.rollback(target_config, failed_digest="b" * 40, reason="smoke failed")

    assert result.status == DeploymentStatus.FAILED
    assert result.restored_digest is None


def test_real_connection_uses_ftps_and_requires_credentials(monkeypatch) -> None:
    import ftplib

    calls: list[str] = []

    class _FakeTLS:
        def connect(self, host, port, timeout):
            calls.append(f"connect:{host}:{port}")

        def login(self, user, password):
            calls.append(f"login:{user}")

        def prot_p(self):
            calls.append("prot_p")

    monkeypatch.setattr(ftplib, "FTP_TLS", _FakeTLS)
    config = TargetConfig(project_id="atrium", target_type="hostinger_ftp", metadata={})
    adapter = FtpMirrorDeploymentAdapter()

    for name in ("FTP_HOST", "FTP_USER", "FTP_PASS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="FTP_HOST, FTP_USER, FTP_PASS"):
        adapter._connect(config)

    monkeypatch.setenv("FTP_HOST", "ftp.example.com")
    monkeypatch.setenv("FTP_USER", "deploy")
    monkeypatch.setenv("FTP_PASS", "secret")
    adapter._connect(config)
    assert calls == ["connect:ftp.example.com:21", "login:deploy", "prot_p"]
