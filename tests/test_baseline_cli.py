"""CLI-level acceptance tests for HF-01-05."""

import json
from pathlib import Path
import subprocess
import sys

from core.planning.baseline_cli import main
from core.planning.baseline_models import BaselineManifest, BaselineSnapshot
from core.planning.baseline_verify import verify_snapshot


def write_fixture_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "source.md").write_text(
        "# Plan\n\n| ID | Title | Status |\n| --- | --- | --- |\n| ITEM-1 | First Item | planned |\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "source_id": "source",
                    "kind": "markdown_table",
                    "relative_path": "source.md",
                    "required": True,
                    "section_heading": None,
                    "table_header": ["ID", "Title", "Status"],
                    "item_id_column": "ID",
                }
            ]
        ),
        encoding="utf-8",
    )
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": []}), encoding="utf-8")
    return tmp_path, catalog, claims


def test_collect_writes_four_sanitized_outputs_and_verify_replays_hashes(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    base_sha = "a" * 40

    assert main(
        [
            "collect",
            "--root",
            str(root),
            "--catalog",
            str(catalog),
            "--claims",
            str(claims),
            "--base-sha",
            base_sha,
            "--out",
            str(output),
        ]
    ) == 0
    assert {path.name for path in output.iterdir()} == {"snapshot.json", "BASELINE.md", "sources.json", "manifest.json"}
    assert "# Plan" not in (output / "sources.json").read_text(encoding="utf-8")
    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 0
    assert "BASELINE_VERIFY_PASS" in capsys.readouterr().out


def test_collect_refuses_to_overwrite_an_existing_output_directory(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "existing"
    output.mkdir()
    (output / "snapshot.json").write_text("keep", encoding="utf-8")

    assert main(
        [
            "collect",
            "--root",
            str(root),
            "--catalog",
            str(catalog),
            "--claims",
            str(claims),
            "--base-sha",
            "a" * 40,
            "--out",
            str(output),
        ]
    ) == 2
    assert (output / "snapshot.json").read_text(encoding="utf-8") == "keep"
    assert "OUTPUT_EXISTS" in capsys.readouterr().err


def test_verify_rejects_a_tampered_fingerprint(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["source_fingerprint"] = "0" * 64
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in capsys.readouterr().err


def test_verify_passes_for_recomputed_intact_baseline(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    base_sha = "a" * 40
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", base_sha, "--out", str(output)]) == 0
    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root), "--manifest", str(output / "manifest.json")]) == 0
    assert "BASELINE_VERIFY_PASS" in capsys.readouterr().out


def test_verify_rejects_forged_readiness(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["hf02_readiness"] = "ready"
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "READINESS_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_forged_verified_dimensions(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(data["items"]) > 0
    data["items"][0]["integration"] = "verified"
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert "ITEM_DIMENSION_MISMATCH" in err


def test_verify_rejects_wiped_blockers(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["blocker_codes"] = ["FAKE_BLOCKER"]
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "BLOCKER_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_nonexistent_evidence_reference(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["items"].append({
        "item_id": "T-GHOST",
        "title": "Ghost",
        "declared_status": "planned",
        "implementation": "unknown",
        "integration": "unknown",
        "operation": "unknown",
        "evidence_ids": ["nonexistent-evidence-id"],
        "issue_ids": [],
        "dependencies": [],
    })
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "EVIDENCE_REFERENCE_INVALID" in capsys.readouterr().err


def test_verify_rejects_self_cycle_dependency(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["items"].append({
        "item_id": "CYCLE-1",
        "title": "Cycle 1",
        "declared_status": "planned",
        "implementation": "unknown",
        "integration": "unknown",
        "operation": "unknown",
        "evidence_ids": [],
        "issue_ids": [],
        "dependencies": ["CYCLE-1"],
    })
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "DEPENDENCY_CYCLE_UNRESOLVED" in capsys.readouterr().err


def test_verify_rejects_tampered_manifest_hash(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    manifest_path = output / "manifest.json"
    m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    m_data["catalog_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(m_data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root), "--manifest", str(manifest_path)]) == 3
    assert "CATALOG_HASH_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_changed_source_file_content(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    # Mutate source file content on disk after collection
    (root / "source.md").write_text("# mutated content\n", encoding="utf-8")

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 3
    assert "SOURCE_HASH_CHANGED" in capsys.readouterr().err


def test_verify_missing_source_file_exits_with_code_2(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    # Remove the source file from disk
    (root / "source.md").unlink()

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 2
    assert "SOURCE_NOT_REPLAYABLE" in capsys.readouterr().err


def test_verify_without_replay_manifest_fails_closed(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    (output / "manifest.json").unlink()

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 2
    assert "REPLAY_DATA_REQUIRED" in capsys.readouterr().err


def test_verify_rejects_forged_item_dependencies(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["items"][0]["dependencies"] = ["EXTRA-DEP"]
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "ITEM_DEPENDENCY_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_forged_claims_in_snapshot(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["claims"].append({
        "claim_id": "forged-claim",
        "item_id": "ITEM-1",
        "dimension": "implementation",
        "assertion": "positive",
        "evidence_kind": "document",
        "source_id": "source",
        "locator": "source.md#L1",
        "source_hash": "a" * 64,
        "scope": "test",
        "summary": "forged claim summary",
    })
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    assert "CLAIMS_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_tampered_manifest_base_sha(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    manifest_path = output / "manifest.json"
    m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    m_data["base_sha"] = "f" * 40
    manifest_path.write_text(json.dumps(m_data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root), "--manifest", str(manifest_path)]) == 3
    assert "MANIFEST_BASE_SHA_MISMATCH" in capsys.readouterr().err


def test_verify_rejects_tampered_manifest_source_paths(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    manifest_path = output / "manifest.json"
    m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    m_data["source_relative_paths"] = ["fake_file.md"]
    manifest_path.write_text(json.dumps(m_data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root), "--manifest", str(manifest_path)]) == 3
    assert "MANIFEST_SOURCE_PATHS_MISMATCH" in capsys.readouterr().err


def test_verify_missing_catalog_file_exits_with_code_2(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    catalog.unlink()

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 2
    assert "CATALOG_MISSING" in capsys.readouterr().err


def test_verify_missing_claims_file_exits_with_code_2(tmp_path: Path, capsys) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0
    claims.unlink()

    assert main(["verify", "--snapshot", str(output / "snapshot.json"), "--root", str(root)]) == 2
    assert "CLAIMS_MISSING" in capsys.readouterr().err


def test_verify_snapshot_api_directly(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0

    snapshot = BaselineSnapshot.model_validate_json((output / "snapshot.json").read_bytes())
    manifest = BaselineManifest.model_validate_json((output / "manifest.json").read_bytes())

    # 1. Valid intact snapshot with root & manifest passes
    report = verify_snapshot(snapshot, root=root, manifest=manifest)
    assert report.valid is True
    assert report.exit_code == 0
    assert report.replayed is True
    assert report.item_count == 1
    assert report.source_count == 1

    # 2. Missing root/manifest fails closed with exit 2
    report_no_manifest = verify_snapshot(snapshot, root=root, manifest=None)
    assert report_no_manifest.valid is False
    assert report_no_manifest.exit_code == 2
    assert report_no_manifest.replayed is False

    # 3. Forged readiness fails with exit 3
    tampered_dict = snapshot.model_dump(mode="json")
    tampered_dict["hf02_readiness"] = "ready"
    tampered_snapshot = BaselineSnapshot.model_validate(tampered_dict)
    report_tampered = verify_snapshot(tampered_snapshot, root=root, manifest=manifest)
    assert report_tampered.valid is False
    assert report_tampered.exit_code == 3
    assert any("READINESS_MISMATCH" in err for err in report_tampered.errors)


def test_reproduce_f04_finding_now_fails_with_corruption_exit_3(tmp_path: Path, capsys) -> None:
    """Exact reproduction of finding F04: previously passed with code 0, must now fail with exit 3."""
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    assert main(["collect", "--root", str(root), "--catalog", str(catalog), "--claims", str(claims), "--base-sha", "a" * 40, "--out", str(output)]) == 0

    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # Perform all F04 forgeries: remove blockers/issues, verify dimensions, ready readiness, fake evidence, cycle
    data["hf02_readiness"] = "ready"
    data["blocker_codes"] = []
    data["issues"] = []
    data["items"][0].update(
        integration="verified",
        operation="verified",
        evidence_ids=["nonexistent-claim"],
        dependencies=[data["items"][0]["item_id"]],
    )
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    assert main(["verify", "--snapshot", str(snapshot_path), "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert "BASELINE_CORRUPTED" in err


def test_subprocess_collect_success_exit_0(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    assert {path.name for path in output.iterdir()} == {"snapshot.json", "BASELINE.md", "sources.json", "manifest.json"}
    summary = json.loads(proc.stdout)
    assert summary["snapshot_id"] is not None
    assert summary["source_count"] == 1
    assert summary["item_count"] == 1
    assert proc.stderr == ""


def test_subprocess_collect_optional_source_missing_exit_0(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    catalog.write_text(
        json.dumps(
            [
                {
                    "source_id": "req_source",
                    "kind": "markdown_table",
                    "relative_path": "source.md",
                    "required": True,
                    "section_heading": None,
                    "table_header": ["ID", "Title", "Status"],
                    "item_id_column": "ID",
                },
                {
                    "source_id": "opt_source",
                    "kind": "markdown_table",
                    "relative_path": "optional_missing.md",
                    "required": False,
                    "section_heading": None,
                    "table_header": ["ID", "Title", "Status"],
                    "item_id_column": "ID",
                },
            ]
        ),
        encoding="utf-8",
    )
    output = root / "run_opt"
    cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    assert {path.name for path in output.iterdir()} == {"snapshot.json", "BASELINE.md", "sources.json", "manifest.json"}
    snap_data = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    obs_map = {obs["source_id"]: obs for obs in snap_data["source_observations"]}
    assert obs_map["req_source"]["status"] == "read"
    assert obs_map["opt_source"]["status"] == "missing"


def test_subprocess_collect_required_source_missing_exit_2_with_partial_snapshot(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    catalog.write_text(
        json.dumps(
            [
                {
                    "source_id": "req_missing",
                    "kind": "markdown_table",
                    "relative_path": "nonexistent.md",
                    "required": True,
                    "section_heading": None,
                    "table_header": ["ID", "Title", "Status"],
                    "item_id_column": "ID",
                }
            ]
        ),
        encoding="utf-8",
    )
    output = root / "run_missing_req"
    cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2
    # CR-12 requirement: Partial report must be preserved on disk!
    assert {path.name for path in output.iterdir()} == {"snapshot.json", "BASELINE.md", "sources.json", "manifest.json"}
    summary = json.loads(proc.stdout)
    assert summary["snapshot_id"] is not None
    assert "REQUIRED_SOURCE_MISSING" in proc.stderr
    assert "[BASELINE_ERROR]" in proc.stderr


def test_subprocess_verify_success_exit_0(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    collect_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    assert subprocess.run(collect_cmd, capture_output=True, text=True, encoding="utf-8").returncode == 0

    verify_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "verify",
        "--snapshot",
        str(output / "snapshot.json"),
        "--root",
        str(root),
        "--manifest",
        str(output / "manifest.json"),
    ]
    proc = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0
    assert "[BASELINE_VERIFY_PASS]" in proc.stdout
    assert proc.stderr == ""


def test_subprocess_verify_missing_source_exit_2(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    collect_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    assert subprocess.run(collect_cmd, capture_output=True, text=True, encoding="utf-8").returncode == 0
    (root / "source.md").unlink()

    verify_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "verify",
        "--snapshot",
        str(output / "snapshot.json"),
        "--root",
        str(root),
    ]
    proc = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2
    assert "SOURCE_NOT_REPLAYABLE" in proc.stderr
    assert "[BASELINE_ERROR]" in proc.stderr


def test_subprocess_verify_required_source_missing_in_snapshot_exit_2(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    catalog.write_text(
        json.dumps(
            [
                {
                    "source_id": "req_missing",
                    "kind": "markdown_table",
                    "relative_path": "nonexistent.md",
                    "required": True,
                    "section_heading": None,
                    "table_header": ["ID", "Title", "Status"],
                    "item_id_column": "ID",
                }
            ]
        ),
        encoding="utf-8",
    )
    output = root / "run_missing_req"
    collect_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    subprocess.run(collect_cmd, capture_output=True, text=True, encoding="utf-8")

    verify_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "verify",
        "--snapshot",
        str(output / "snapshot.json"),
        "--root",
        str(root),
    ]
    proc = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2
    assert "REQUIRED_SOURCE_MISSING" in proc.stderr
    assert "[BASELINE_ERROR]" in proc.stderr


def test_subprocess_verify_corrupted_snapshot_exit_3(tmp_path: Path) -> None:
    root, catalog, claims = write_fixture_root(tmp_path)
    output = root / "run"
    collect_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "collect",
        "--root",
        str(root),
        "--catalog",
        str(catalog),
        "--claims",
        str(claims),
        "--base-sha",
        "a" * 40,
        "--out",
        str(output),
    ]
    assert subprocess.run(collect_cmd, capture_output=True, text=True, encoding="utf-8").returncode == 0

    snapshot_path = output / "snapshot.json"
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    data["hf02_readiness"] = "ready"
    snapshot_path.write_text(json.dumps(data), encoding="utf-8")

    verify_cmd = [
        sys.executable,
        "-m",
        "core.planning.baseline_cli",
        "verify",
        "--snapshot",
        str(snapshot_path),
        "--root",
        str(root),
    ]
    proc = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 3
    assert "[BASELINE_CORRUPTED]" in proc.stderr
    assert "READINESS_MISMATCH" in proc.stderr


def test_subprocess_argparse_normal_behavior() -> None:
    # 1. --help exits 0
    proc_help = subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", "--help"], capture_output=True, text=True, encoding="utf-8")
    assert proc_help.returncode == 0
    assert "usage:" in proc_help.stdout or "usage:" in proc_help.stderr

    proc_collect_help = subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", "collect", "--help"], capture_output=True, text=True, encoding="utf-8")
    assert proc_collect_help.returncode == 0

    proc_verify_help = subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", "verify", "--help"], capture_output=True, text=True, encoding="utf-8")
    assert proc_verify_help.returncode == 0

    # 2. Missing required arguments or unknown command exits 2 (argparse standard)
    proc_missing_args = subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", "collect"], capture_output=True, text=True, encoding="utf-8")
    assert proc_missing_args.returncode == 2

    proc_unknown = subprocess.run([sys.executable, "-m", "core.planning.baseline_cli", "invalid_subcmd"], capture_output=True, text=True, encoding="utf-8")
    assert proc_unknown.returncode == 2

