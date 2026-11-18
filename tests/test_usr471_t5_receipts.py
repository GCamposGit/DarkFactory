"""T5 receipt destinations and line_validate snapshot hygiene for USR-471."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import scripts.line_validate as line_validate
from core.line import fleet_policy
from core.models import audit

ROOT = Path(__file__).resolve().parents[1]


def test_sidecar_producers_use_state_root_and_preserve_legacy_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    state_root = tmp_path / "persistent-state"
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("STATE_ROOT", str(state_root))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)

    legacy_runs = project_root / ".factory" / "runs"
    legacy_runs.mkdir(parents=True)
    legacy_snapshot = legacy_runs / "fleet_policy_snapshot.json"
    legacy_receipt = legacy_runs / "model_audit_receipt.json"
    legacy_snapshot.write_text('{"legacy": "snapshot"}\n', encoding="utf-8")
    legacy_receipt.write_text('{"legacy": "receipt"}\n', encoding="utf-8")

    snapshot = {"schema_version": fleet_policy.SNAPSHOT_SCHEMA_VERSION, "hosts": []}
    fleet_target = fleet_policy.write_effective_policy_snapshot(snapshot)
    assert fleet_target == state_root / "runs" / "fleet_policy_snapshot.json"
    assert json.loads(fleet_target.read_text(encoding="utf-8")) == snapshot

    ledger_target = state_root / "runs" / "model_audit" / "USR-471.jsonl"
    sample = audit.ExecutionSample(
        execution_id="exec-t5",
        ticket_id="USR-471",
        stage="review",
        harness="openrouter",
        latency_ms=5,
        timeout_sec=30,
        schema_valid=False,
        real_delivery=False,
    )
    audit.record_execution_sample(sample, ticket_id="USR-471")
    assert ledger_target.is_file()
    assert json.loads(ledger_target.read_text(encoding="utf-8")) == sample.model_dump(mode="json")

    observed_runs: list[Path] = []

    def read_empty_ledger(runs_dir: Path, _ticket_id: str | None) -> list[dict[str, object]]:
        observed_runs.append(runs_dir)
        return []

    monkeypatch.setattr(audit, "_recorded_samples", read_empty_ledger)
    audit.audit_recorded()
    receipt_target = state_root / "runs" / "model_audit_receipt.json"
    assert observed_runs == [state_root / "runs" / "model_audit"]
    assert json.loads(receipt_target.read_text(encoding="utf-8"))["passed"] is False
    assert not list((state_root / "runs").glob(".fleet_policy_snapshot.json.*.tmp"))
    assert not list((state_root / "runs").glob(".model_audit_receipt.json.*.tmp"))

    assert legacy_snapshot.read_text(encoding="utf-8") == '{"legacy": "snapshot"}\n'
    assert legacy_receipt.read_text(encoding="utf-8") == '{"legacy": "receipt"}\n'


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def test_line_validate_snapshots_sidecars_but_keeps_changed_tracked_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    (root / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    tracked = root / ".factory" / "runs" / "hf01-run" / "evidence.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text('{"ok": true}\n', encoding="utf-8")
    runner = root / "core" / "harness" / "runner.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# fixture runner\n", encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".gitignore", ".factory/runs/hf01-run/evidence.json", "core/harness/runner.py")
    _git(root, "commit", "-qm", "fixture baseline")
    head_before = _git(root, "rev-parse", "HEAD")

    runs = root / ".factory" / "runs"
    (runs / "fleet_policy_snapshot.json").write_text('{"generated": true}\n', encoding="utf-8")
    (runs / ".fleet_policy_snapshot.json.0123.tmp").write_text("partial\n", encoding="utf-8")
    (runs / "model_audit_receipt.json").write_text('{"passed": false}\n', encoding="utf-8")
    (runs / ".model_audit_receipt.json.0456.tmp").write_text("partial\n", encoding="utf-8")
    tracked.write_text('{"ok": false}\n', encoding="utf-8")

    observed: list[Path] = []

    def passing_runner(args: list[str], *, cwd: Path, env: dict[str, str]) -> int:
        del env
        checkout = Path(cwd)
        observed.append(checkout)
        assert args[-1] == "--quick"
        assert not (checkout / ".factory/runs/fleet_policy_snapshot.json").exists()
        assert not (checkout / ".factory/runs/model_audit_receipt.json").exists()
        assert not (checkout / ".factory/runs/.fleet_policy_snapshot.json.0123.tmp").exists()
        assert not (checkout / ".factory/runs/.model_audit_receipt.json.0456.tmp").exists()
        assert (checkout / ".factory/runs/hf01-run/evidence.json").read_text(encoding="utf-8") == '{"ok": false}\n'
        return 0

    monkeypatch.setattr(line_validate.subprocess, "call", passing_runner)
    assert line_validate.main(root=root, argv=[]) == 0
    assert len(observed) == 1
    assert _git(root, "rev-parse", "HEAD") == head_before
    assert "M .factory/runs/hf01-run/evidence.json" in _git(root, "status", "--porcelain")

