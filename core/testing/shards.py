"""USR-300: deterministic partition of the test files into CI shards.

``partition_files`` assigns every test file to exactly one of ``total`` shards using a stable hash of
its POSIX relative path (adding a file never moves another file). ``command_for_shard`` rewrites a
pytest command so that the ``tests`` target is replaced by the files of one shard. The runner
(``core/harness/runner.py --shard I/N``) imports this module; shards share no state, the union of all
shards is the full set of files and the intersection of any two is empty.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SHARD_ENV = "DARKFAC_TEST_SHARD"
TESTS_TARGETS = frozenset({"tests", "tests/", "tests\\"})


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse ``"I/N"`` (1-based I) into ``(index0, total)``."""
    try:
        left, right = spec.strip().split("/", 1)
        index, total = int(left), int(right)
    except ValueError as exc:
        raise ValueError(f"invalid shard {spec!r}; expected I/N such as 2/4") from exc
    if total < 1 or not 1 <= index <= total:
        raise ValueError(f"invalid shard {spec!r}; need 1 <= I <= N")
    return index - 1, total


def shard_of(path: str, total: int) -> int:
    """Stable shard (0-based) of a POSIX relative path."""
    digest = hashlib.sha256(path.replace("\\", "/").encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % total


def partition_files(files: list[str], index: int, total: int) -> list[str]:
    """Files of shard ``index`` (0-based), sorted."""
    if total < 1 or not 0 <= index < total:
        raise ValueError("shard index out of range")
    return sorted(f for f in set(files) if shard_of(f, total) == index)


def discover_test_files(root: Path, ignored: frozenset[str] = frozenset()) -> list[str]:
    """All ``test_*.py`` files under ``root/tests`` (POSIX relative), minus ignored paths."""
    tests_dir = root / "tests"
    found = sorted(p.relative_to(root).as_posix() for p in tests_dir.rglob("test_*.py"))
    return [f for f in found if not _is_ignored(f, ignored)]


def _is_ignored(path: str, ignored: frozenset[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in ignored)


def _ignored_paths(command: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for i, token in enumerate(command):
        if token.startswith("--ignore="):
            out.add(token.split("=", 1)[1].replace("\\", "/"))
        elif token == "--ignore" and i + 1 < len(command):
            out.add(command[i + 1].replace("\\", "/"))
    return frozenset(out)


def command_for_shard(command: list[str], spec: str, root: Path) -> list[str]:
    """Replace the ``tests`` target of a pytest command by the files of the selected shard.

    Commands without a bare ``tests`` target (compileall, lint, ...) are returned unchanged.
    """
    if not any(token in TESTS_TARGETS for token in command):
        return command
    index, total = parse_shard(spec)
    files = partition_files(discover_test_files(root, _ignored_paths(command)), index, total)
    rewritten: list[str] = []
    replaced = False
    for token in command:
        if token in TESTS_TARGETS and not replaced:
            rewritten.extend(files)
            replaced = True
        else:
            rewritten.append(token)
    return rewritten
