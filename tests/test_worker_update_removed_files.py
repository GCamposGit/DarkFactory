"""USR-196: /system/update do worker sobrevive a arquivos rastreados que o upstream deixou de versionar.

Usa repositorios git reais em ``tmp_path`` (origin bare + seed + checkout do worker), sem rede.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.harness import remote_worker as rw

STATE = ".factory/state.json"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return result.stdout.strip()


def _write(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")


@dataclass
class Scenario:
    seed: Path
    worker: Path

    def head_of_seed(self) -> str:
        return _git(self.seed, "rev-parse", "HEAD")

    def stop_versioning_state(self) -> None:
        """Upstream passa a ignorar ``.factory/state.json`` (git rm --cached + .gitignore)."""
        _git(self.seed, "rm", "--cached", "-q", STATE)
        _write(self.seed, ".gitignore", STATE + "\n")
        _git(self.seed, "add", ".gitignore")
        _git(self.seed, "commit", "-q", "-m", "stop versioning state.json")
        _git(self.seed, "push", "-q", "origin", "main")

    def client(self) -> TestClient:
        return TestClient(rw.create_worker_app(project_root=self.worker, node_id="usr196-test"))


@pytest.fixture()
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    monkeypatch.delenv(rw.ENV_WORKER_TOKEN, raising=False)
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write(seed, STATE, "upstream-v1\n")
    _write(seed, "keep.txt", "keep-v1\n")
    _write(seed, "gone.txt", "gone-v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(worker)], check=True)
    return Scenario(seed=seed, worker=worker)


def _backups(worker: Path) -> list[Path]:
    base = worker / ".factory" / "backups" / "worker_update"
    return sorted(base.glob("*/**/*")) if base.exists() else []


def test_update_preserves_copy_and_discards_local_change_of_file_no_longer_versioned(scenario: Scenario) -> None:
    scenario.stop_versioning_state()
    _write(scenario.worker, STATE, "local-edit\n")
    _write(scenario.worker, "keep.txt", "local-keep\n")  # continua versionado: nao pode ser tocado

    data = scenario.client().post("/system/update", json={}).json()

    assert data["success"] is True, data["output"]
    assert data["current_commit"] == scenario.head_of_seed()
    saved = [p for p in _backups(scenario.worker) if p.is_file()]
    assert [p.name for p in saved] == ["state.json"]
    assert saved[0].read_text(encoding="utf-8") == "local-edit\n"  # copia fiel da modificacao local
    assert "USR-196" in data["output"] and STATE in data["output"]
    assert (scenario.worker / "keep.txt").read_text(encoding="utf-8") == "local-keep\n"
    assert STATE not in _git(scenario.worker, "ls-files").splitlines()


def test_update_does_not_back_up_removed_file_without_local_changes(scenario: Scenario) -> None:
    scenario.stop_versioning_state()

    data = scenario.client().post("/system/update", json={}).json()

    assert data["success"] is True, data["output"]
    assert data["current_commit"] == scenario.head_of_seed()
    assert _backups(scenario.worker) == []
    assert "USR-196" not in data["output"]


def test_update_never_discards_changes_of_files_that_stay_versioned(scenario: Scenario) -> None:
    _write(scenario.seed, "keep.txt", "keep-upstream-v2\n")
    _git(scenario.seed, "commit", "-q", "-am", "change keep.txt")
    _git(scenario.seed, "push", "-q", "origin", "main")
    _write(scenario.worker, "keep.txt", "local-keep\n")

    data = scenario.client().post("/system/update", json={}).json()

    assert data["success"] is False  # o git recusa; o worker nao decide descartar o que continua versionado
    assert (scenario.worker / "keep.txt").read_text(encoding="utf-8") == "local-keep\n"
    assert _backups(scenario.worker) == []
    assert "overwritten" in data["output"]  # o stderr real do git segue no output (USR-196 item 1)


def test_update_keeps_local_file_when_backup_copy_fails(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario.stop_versioning_state()
    _write(scenario.worker, STATE, "local-edit\n")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rw.shutil, "copy2", boom)
    data = scenario.client().post("/system/update", json={}).json()

    assert data["success"] is False
    assert (scenario.worker / STATE).read_text(encoding="utf-8") == "local-edit\n"  # sem copia, nada e descartado
    assert "backup failed" in data["output"]


def test_preserve_helper_is_noop_without_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "a.txt", "a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _write(repo, "a.txt", "changed\n")

    assert rw.preserve_files_unversioned_upstream(repo) == []
    assert (repo / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_preserve_helper_never_raises_outside_a_repository(tmp_path: Path) -> None:
    assert rw.preserve_files_unversioned_upstream(tmp_path) == []
