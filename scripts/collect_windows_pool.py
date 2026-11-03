#!/usr/bin/env python3
"""Collect a read-only snapshot of Windows memory pools and pool tags.

The collector never changes system configuration. It emits one JSON object so
successive invocations can be stored as JSON Lines and compared over time.

PoolMon is optional. Microsoft ships poolmon.exe with the Windows Driver Kit:
https://learn.microsoft.com/windows-hardware/drivers/devtest/poolmon
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance -ClassName Win32_OperatingSystem
$memory = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Memory
[ordered]@{
  boot_time = $os.LastBootUpTime.ToUniversalTime().ToString('o')
  total_physical_memory_bytes = [uint64]$os.TotalVisibleMemorySize * 1024
  available_memory_bytes = [uint64]$memory.AvailableBytes
  pool_paged_bytes = [uint64]$memory.PoolPagedBytes
  pool_nonpaged_bytes = [uint64]$memory.PoolNonpagedBytes
  pool_paged_resident_bytes = if (
    $null -ne $memory.PSObject.Properties['PoolPagedResidentBytes']
  ) { [uint64]$memory.PoolPagedResidentBytes } else { $null }
} | ConvertTo-Json -Compress
""".strip()

POOLMON_HEADERS = {
    "tag",
    "type",
    "allocs",
    "frees",
    "diff",
    "bytes",
    "per alloc",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def diagnostic(code: str, message: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    result.update(extra)
    return result


def _parse_integer(value: str) -> int:
    cleaned = value.strip().replace(",", "").replace(".", "")
    if not cleaned or not re.fullmatch(r"-?\d+", cleaned):
        raise ValueError(f"not an integer: {value!r}")
    return int(cleaned)


def parse_poolmon_output(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse PoolMon's whitespace table without relying on localized headings."""

    tags: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        columns = line.split()
        lowered = {column.lower() for column in columns}
        if POOLMON_HEADERS.intersection(lowered) or line.startswith(("Memory:", "Pool N:")):
            continue

        # Expected PoolMon row:
        # Tag Type Allocs (delta) Frees (delta) Diff Bytes Per Alloc
        match = re.match(
            r"^(?P<tag>\S{1,4})\s+"
            r"(?P<type>Nonp|Paged)\s+"
            r"(?P<allocs>[\d,.]+)(?:\s+\(\s*-?[\d,.]+\))?\s+"
            r"(?P<frees>[\d,.]+)(?:\s+\(\s*-?[\d,.]+\))?\s+"
            r"(?P<diff>-?[\d,.]+)\s+"
            r"(?P<bytes>[\d,.]+)(?:\s+[\d,.]+)?\s*$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue

        try:
            allocs = _parse_integer(match.group("allocs"))
            frees = _parse_integer(match.group("frees"))
            reported_diff = _parse_integer(match.group("diff"))
            byte_count = _parse_integer(match.group("bytes"))
        except ValueError:
            diagnostics.append(
                diagnostic(
                    "poolmon_row_invalid",
                    "PoolMon returned a row with an invalid numeric field.",
                    row=raw_line,
                )
            )
            continue

        calculated_diff = allocs - frees
        row: dict[str, Any] = {
            "tag": match.group("tag"),
            "pool_type": (
                "nonpaged" if match.group("type").lower() == "nonp" else "paged"
            ),
            "bytes": byte_count,
            "allocations": allocs,
            "frees": frees,
            "allocation_free_difference": reported_diff,
        }
        if calculated_diff != reported_diff:
            diagnostics.append(
                diagnostic(
                    "poolmon_difference_mismatch",
                    "PoolMon's reported difference does not equal allocations minus frees.",
                    tag=row["tag"],
                    reported=reported_diff,
                    calculated=calculated_diff,
                )
            )
        tags.append(row)

    tags.sort(key=lambda item: item["bytes"], reverse=True)
    if not tags:
        diagnostics.append(
            diagnostic(
                "poolmon_no_tags",
                "No pool tag rows were parsed; verify PoolMon version, privileges, "
                "and output format.",
            )
        )
    return tags, diagnostics


def collect_memory(
    executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        completed = runner(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                POWERSHELL_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {}, [
            diagnostic(
                "memory_collection_failed",
                "Could not execute PowerShell/CIM memory collection.",
                detail=str(error),
            )
        ]

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return {}, [
            diagnostic(
                "memory_collection_failed",
                "PowerShell could not read Windows memory counters. Check CIM/WMI "
                "health and the account's permissions.",
                detail=detail,
            )
        ]

    try:
        raw = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        return {}, [
            diagnostic(
                "memory_output_invalid",
                "PowerShell returned invalid JSON for memory counters.",
                detail=str(error),
            )
        ]

    fields = {
        "total_physical_memory_bytes": raw.get("total_physical_memory_bytes"),
        "available_memory_bytes": raw.get("available_memory_bytes"),
        "pool_paged_bytes": raw.get("pool_paged_bytes"),
        "pool_paged_resident_bytes": raw.get("pool_paged_resident_bytes"),
        "pool_nonpaged_bytes": raw.get("pool_nonpaged_bytes"),
    }
    memory: dict[str, Any] = {"unit": "bytes"}
    diagnostics: list[dict[str, Any]] = []
    for name, value in fields.items():
        if value is None:
            diagnostics.append(
                diagnostic(
                    "memory_field_unavailable",
                    f"Windows did not expose optional memory field {name}.",
                    field=name,
                )
            )
            continue
        try:
            memory[name] = int(value)
        except (TypeError, ValueError):
            diagnostics.append(
                diagnostic(
                    "memory_field_invalid",
                    f"Windows returned a non-numeric value for {name}.",
                    field=name,
                )
            )

    boot_time = raw.get("boot_time")
    if boot_time:
        memory["boot_time"] = str(boot_time)
    else:
        diagnostics.append(
            diagnostic(
                "boot_time_unavailable",
                "Windows did not expose the last boot time.",
            )
        )
    return memory, diagnostics


def collect_pool_tags(
    executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        # -b sorts by bytes, -n avoids continuously refreshing the console.
        completed = runner(
            [executable, "-b", "-n"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], [
            diagnostic(
                "poolmon_timeout",
                "PoolMon did not finish within 30 seconds.",
            )
        ]
    except OSError as error:
        return [], [
            diagnostic(
                "poolmon_execution_failed",
                "PoolMon could not be executed.",
                detail=str(error),
            )
        ]

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return [], [
            diagnostic(
                "poolmon_failed",
                "PoolMon failed. Run from an elevated terminal and verify that "
                "pool tagging is available.",
                return_code=completed.returncode,
                detail=detail,
            )
        ]
    return parse_poolmon_output(completed.stdout)


def _find_executable(explicit: str | None, names: Sequence[str]) -> str | None:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        located = shutil.which(explicit)
        return located
    for name in names:
        located = shutil.which(name)
        if located:
            return located
    return None


def build_snapshot(
    *,
    system: str | None = None,
    poolmon_path: str | None = None,
    now: Callable[[], datetime] = utc_now,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    current_system = system or platform.system()
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": {
            "platform": current_system,
            "hostname": platform.node(),
        },
        "memory": {"unit": "bytes"},
        "pool_tags": {
            "unit": "bytes",
            "source": "poolmon",
            "entries": [],
        },
        "diagnostics": [],
    }

    if current_system != "Windows":
        snapshot["diagnostics"].append(
            diagnostic(
                "unsupported_platform",
                "Windows memory pools can only be collected on Windows.",
                detected_platform=current_system,
            )
        )
        return snapshot

    powershell = _find_executable(None, ("powershell.exe", "pwsh.exe"))
    if powershell is None:
        snapshot["diagnostics"].append(
            diagnostic(
                "powershell_not_found",
                "PowerShell was not found; install/restore PowerShell or add it to PATH.",
            )
        )
    else:
        memory, memory_diagnostics = collect_memory(powershell, runner)
        if memory:
            snapshot["memory"] = memory
        snapshot["diagnostics"].extend(memory_diagnostics)

    poolmon = _find_executable(poolmon_path, ("poolmon.exe", "poolmon"))
    if poolmon is None:
        snapshot["diagnostics"].append(
            diagnostic(
                "poolmon_not_found",
                "PoolMon was not found. Install the Windows Driver Kit and pass "
                "--poolmon PATH or add poolmon.exe to PATH.",
            )
        )
    else:
        tags, tag_diagnostics = collect_pool_tags(poolmon, runner)
        snapshot["pool_tags"]["entries"] = tags
        snapshot["diagnostics"].extend(tag_diagnostics)

    return snapshot


def write_snapshot(
    snapshot: Mapping[str, Any],
    output: str,
    *,
    append: bool,
) -> None:
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    if output == "-":
        print(serialized)
        return

    mode = "a" if append else "w"
    with open(output, mode, encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)
        stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect a read-only Windows kernel memory and PoolMon snapshot."
    )
    parser.add_argument(
        "--poolmon",
        help="Path to poolmon.exe. If omitted, PATH is searched.",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output JSON/JSONL file, or '-' for stdout (default).",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append one JSON line instead of replacing the output file.",
    )
    args = parser.parse_args(argv)

    if args.append and args.output == "-":
        parser.error("--append requires a file passed with --output")

    snapshot = build_snapshot(poolmon_path=args.poolmon)
    try:
        write_snapshot(snapshot, args.output, append=args.append)
    except OSError as error:
        print(f"could not write snapshot: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
