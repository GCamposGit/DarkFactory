import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "collect_windows_pool.py"
SPEC = importlib.util.spec_from_file_location("collect_windows_pool", MODULE_PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def test_parse_poolmon_output_normalizes_and_sorts_tags():
    output = """
 Memory: 33554432K Avail: 4194304K
 Tag  Type     Allocs            Frees            Diff       Bytes
 MmSt Nonp       1200 ( 0)         200 ( 0)       1000      8192000 8192
 File Paged        20 ( 1)          15 ( 0)          5        40960 8192
 """
    tags, diagnostics = collector.parse_poolmon_output(output)

    assert diagnostics == []
    assert tags == [
        {
            "tag": "MmSt",
            "pool_type": "nonpaged",
            "bytes": 8192000,
            "allocations": 1200,
            "frees": 200,
            "allocation_free_difference": 1000,
        },
        {
            "tag": "File",
            "pool_type": "paged",
            "bytes": 40960,
            "allocations": 20,
            "frees": 15,
            "allocation_free_difference": 5,
        },
    ]


def test_parse_poolmon_output_reports_empty_capture():
    tags, diagnostics = collector.parse_poolmon_output("Poolmon Version 10\n")

    assert tags == []
    assert diagnostics[0]["code"] == "poolmon_no_tags"


def test_collect_memory_preserves_bytes_and_optional_field_absence():
    payload = {
        "boot_time": "2026-10-28T06:56:00.0000000Z",
        "total_physical_memory_bytes": 34037645312,
        "available_memory_bytes": 4294967296,
        "pool_paged_bytes": 16310116352,
        "pool_paged_resident_bytes": None,
        "pool_nonpaged_bytes": 2061584302,
    }

    def runner(*args, **kwargs):
        return CompletedProcess(args[0], 0, json.dumps(payload), "")

    memory, diagnostics = collector.collect_memory("powershell.exe", runner)

    assert memory["unit"] == "bytes"
    assert memory["total_physical_memory_bytes"] == 34037645312
    assert memory["available_memory_bytes"] == 4294967296
    assert memory["pool_paged_bytes"] == 16310116352
    assert memory["pool_nonpaged_bytes"] == 2061584302
    assert "pool_paged_resident_bytes" not in memory
    assert memory["boot_time"] == "2026-10-28T06:56:00.0000000Z"
    assert any(
        item["code"] == "memory_field_unavailable"
        and item["field"] == "pool_paged_resident_bytes"
        for item in diagnostics
    )


def test_non_windows_snapshot_is_partial_and_actionable():
    instant = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    snapshot = collector.build_snapshot(system="Linux", now=lambda: instant)

    assert snapshot["timestamp"] == "2026-11-03T12:00:00Z"
    assert snapshot["memory"] == {"unit": "bytes"}
    assert snapshot["pool_tags"]["entries"] == []
    assert snapshot["diagnostics"][0]["code"] == "unsupported_platform"


def test_poolmon_failure_returns_partial_diagnostic():
    def runner(*args, **kwargs):
        return CompletedProcess(args[0], 5, "", "Access is denied")

    tags, diagnostics = collector.collect_pool_tags("poolmon.exe", runner)

    assert tags == []
    assert diagnostics == [
        {
            "code": "poolmon_failed",
            "message": (
                "PoolMon failed. Run from an elevated terminal and verify that "
                "pool tagging is available."
            ),
            "return_code": 5,
            "detail": "Access is denied",
        }
    ]


def test_write_snapshot_append_produces_json_lines(tmp_path):
    output = tmp_path / "samples.jsonl"
    snapshot = {"timestamp": "2026-11-03T12:00:00Z"}

    collector.write_snapshot(snapshot, str(output), append=True)
    collector.write_snapshot(snapshot, str(output), append=True)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line) == snapshot for line in lines)
