"""Isolar artefatos operacionais da arvore candidata (USR-471 T6, USR-561).

Cobre:
- qualquer arquivo nao rastreado sob `.factory/runs/` e ignorado pelo git (USR-561), entao
  snapshots, recibos, logs e estado do despachante nunca viram `gate_dirty_worktree`.
- arquivo nao rastreado fora de `.factory/runs/` continua visivel.
- evidencia ja versionada sob `.factory/runs/` continua visivel ao git e ao portao quando muda.
- Os dois produtores gravam o snapshot/recibo atomicamente (sem temporario remanescente).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import run_ticket
from core.line import fleet_policy
from core.models import audit

ROOT = Path(run_ticket.__file__).resolve().parent

_IGNORED_PATHS = (
    ".factory/runs/fleet_policy_snapshot.json",
    ".factory/runs/.fleet_policy_snapshot.json.sample.tmp",
    ".factory/runs/fleet_policy_snapshot.tmp",
    ".factory/runs/model_audit_receipt.json",
    ".factory/runs/.model_audit_receipt.json.sample.tmp",
    ".factory/runs/model_audit_receipt.tmp",
)


def _check_ignore(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_snapshot_and_receipt_paths_are_ignored() -> None:
    out = _check_ignore(*_IGNORED_PATHS)
    assert sorted(out.stdout.split()) == sorted(_IGNORED_PATHS)


def test_runs_directory_not_broadly_ignored() -> None:
    # A versioned evidence file under the same .factory/runs/ prefix must stay visible to git.
    tracked = ".factory/runs/hf01-20260909T000103Z-1e8bf83f/evidence.json"
    out = _check_ignore(tracked)
    assert out.returncode == 1
    assert out.stdout.strip() == ""


def test_untracked_runs_files_are_ignored_but_code_outside_runs_is_not() -> None:
    for path in (
        ".factory/runs/model_audit/USR-471.jsonl",
        ".factory/runs/review_benchmark.jsonl",
        ".factory/runs/fleet_dispatcher_logs/dispatcher.log",
        ".factory/runs/hf01-run/evidence.json",
    ):
        out = _check_ignore(path)
        assert out.returncode == 0, path
        assert out.stdout.strip() == path

    outside = _check_ignore("core/untracked_module.py")
    assert outside.returncode == 1
    assert outside.stdout.strip() == ""


def test_fleet_policy_snapshot_write_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "fleet_policy_snapshot.json"
    report = {"schema_version": fleet_policy.SNAPSHOT_SCHEMA_VERSION, "hosts": []}
    fleet_policy.write_effective_policy_snapshot(report, path=target)

    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == report
    assert not target.with_suffix(".tmp").exists()


def test_model_audit_receipt_write_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "model_audit_receipt.json"
    report = audit.audit_executions([], receipt_path=target)

    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["passed"] == report.passed
    assert not target.with_suffix(".tmp").exists()


def test_snapshot_paths_are_not_shared_checkout_work() -> None:
    # Untracked runtime snapshots never belong to "another ticket/session's work" -- once
    # ignored, git simply never reports them, so `is_runtime_artifact` need not special-case
    # them (the gitignore rule is the single source of truth, USR-471 T6).
    for path in _IGNORED_PATHS:
        out = _check_ignore(path)
        assert out.returncode == 0, path


def test_sidecar_producers_honor_explicit_state_root(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "persistent-state"
    monkeypatch.setenv("STATE_ROOT", str(state_root))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)

    assert fleet_policy.snapshot_path() == state_root / "runs" / "fleet_policy_snapshot.json"
    assert audit.receipt_path() == state_root / "runs" / "model_audit_receipt.json"


def test_sidecars_do_not_dirty_candidate_but_tracked_runs_evidence_does(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    tracked = tmp_path / ".factory" / "runs" / "hf01-run" / "evidence.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("{\"ok\": true}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".gitignore", ".factory/runs/hf01-run/evidence.json"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    sidecar = tmp_path / ".factory" / "runs" / "fleet_policy_snapshot.json"
    sidecar.write_text("{}\n", encoding="utf-8")
    assert not subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

    tracked.write_text("{\"ok\": false}\n", encoding="utf-8")
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert "M .factory/runs/hf01-run/evidence.json" in changed
    tracked.unlink()
    deleted = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert "D .factory/runs/hf01-run/evidence.json" in deleted
