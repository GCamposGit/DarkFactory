"""CLI-level acceptance tests for HF-01-05."""

import json
from pathlib import Path

from core.planning.baseline_cli import main


def write_fixture_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    (tmp_path / "source.md").write_text("# source\n", encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "source_id": "source",
                    "kind": "source_file",
                    "relative_path": "source.md",
                    "required": True,
                    "section_heading": None,
                    "table_header": None,
                    "item_id_column": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": []}), encoding="utf-8")
    return tmp_path, catalog, claims


def test_collect_writes_three_sanitized_outputs_and_verify_replays_hashes(tmp_path: Path, capsys) -> None:
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
    assert {path.name for path in output.iterdir()} == {"snapshot.json", "BASELINE.md", "sources.json"}
    assert "# source" not in (output / "sources.json").read_text(encoding="utf-8")
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

    assert main(["verify", "--snapshot", str(snapshot_path)]) == 2
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in capsys.readouterr().err
