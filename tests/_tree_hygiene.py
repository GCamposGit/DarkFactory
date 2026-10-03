"""Tree-hygiene guard: the test suite must not alter the checkout it runs in.

The official harness (``core/harness/runner.py``) refuses to bind a verdict to
a candidate SHA when ``git status`` is not clean after the steps ran, but it
only says "worktree is dirty" without naming a single path. This module lets
``tests/conftest.py`` fingerprint the dirty set before and after a pytest
session and name every path a test created, modified or removed, so the
offending test can be fixed (write to ``tmp_path`` or redirect the state
root) instead of guessed at from a CI log.

Fingerprint: ``{path: (porcelain_status, sha256_or_None)}`` built from
``git status --porcelain=v1 -z --untracked-files=all``. Ignored paths are not
listed by git, so a new file under an ignored directory is never reported --
only what would also make the harness reject the run. A tree that was already
dirty before the session and is unchanged after it reports nothing.

Set ``DARKFAC_TREE_HYGIENE=off`` (or ``0``/``false``/``no``) to disable the
guard. Any git failure (no git binary, not a repository, timeout) also
disables it: the guard must never break the suite by itself.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_SWITCH = "DARKFAC_TREE_HYGIENE"
# Set by the session that owns the guard so a pytest launched by a test (a
# nested run) does not compare against files other xdist workers are writing.
ENV_ACTIVE = "DARKFAC_TREE_HYGIENE_ACTIVE"
MAX_HASH_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_SEC = 120.0
REPORT_PREFIX = "[TREE-HYGIENE]"
FAILURE_MESSAGE = (
    "a suite de testes nao pode alterar o checkout; escreva em tmp_path ou "
    "redirecione a raiz de estado (monkeypatch) nos testes listados acima. "
    "Desative apenas para depuracao local com DARKFAC_TREE_HYGIENE=off."
)

# path -> (two-letter porcelain status, content digest or None when the path
# is gone / not a regular file)
Fingerprint = dict[str, tuple[str, str | None]]

_OFF_VALUES = {"off", "0", "false", "no"}
_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Violation:
    """One path whose dirty state differs between session start and finish."""

    kind: str  # "new" | "changed" | "restored"
    status: str  # porcelain status after the session ("--" when clean again)
    path: str

    def render(self) -> str:
        return f"{REPORT_PREFIX} {self.kind} {self.status} {self.path}"


def is_disabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_SWITCH, "").strip().lower() in _OFF_VALUES


def is_nested_session(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(ENV_ACTIVE))


def _content_digest(path: Path, max_hash_bytes: int) -> str | None:
    """sha256 of the content; None when the path no longer exists.

    Files above ``max_hash_bytes`` hash their first ``max_hash_bytes`` plus the
    total size, which still catches appends and most rewrites without reading
    arbitrarily large artifacts.
    """

    try:
        if path.is_symlink():
            return "link:" + hashlib.sha256(os.readlink(path).encode("utf-8", "replace")).hexdigest()
        if not path.exists():
            return None
        if not path.is_file():
            return "dir"
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(max_hash_bytes)
        digest = hashlib.sha256(head)
        if size > max_hash_bytes:
            digest.update(f":{size}".encode("ascii"))
        return digest.hexdigest()
    except OSError:
        return _UNREADABLE


def _parse_porcelain_z(raw: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into ``(status, path)`` pairs.

    Entries are ``XY <path>\\0``; a rename/copy is ``XY <to>\\0<from>\\0`` and
    the trailing source path is dropped (the destination carries the state).
    """

    entries: list[tuple[str, str]] = []
    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2] != " ":
            continue
        status, path = token[:2], token[3:]
        if "R" in status or "C" in status:
            index += 1
        entries.append((status, path))
    return entries


def _scan_factory_dir(root: Path, fingerprint: Fingerprint) -> None:
    """Include unversioned/ignored files under .factory in the fingerprint.

    Tests must never mutate the real .factory state. We use stat size and mtime
    which takes < 0.3s for thousands of files, detecting any created, modified,
    or deleted files under .factory.
    """
    factory_dir = root / ".factory"
    if not factory_dir.is_dir():
        return
    try:
        for entry in os.walk(factory_dir):
            dirpath, _, filenames = entry
            for fname in filenames:
                full_path = Path(dirpath) / fname
                try:
                    st = full_path.stat()
                    rel_path = full_path.relative_to(root).as_posix()
                    if rel_path not in fingerprint:
                        fingerprint[rel_path] = ("!!", f"{st.st_size}:{st.st_mtime_ns}")
                except OSError:
                    continue
    except OSError:
        pass


def take_fingerprint(root: Path, *, max_hash_bytes: int = MAX_HASH_BYTES) -> Fingerprint | None:
    """Fingerprint the dirty set of the git checkout at ``root``.

    Returns None (guard disabled) when git is missing, ``root`` is not a
    repository or the query fails or times out.
    """

    command = [
        "git",
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None

    fingerprint: Fingerprint = {}
    for status, path in _parse_porcelain_z(process.stdout):
        fingerprint[path] = (status, _content_digest(root / path, max_hash_bytes))
    _scan_factory_dir(root, fingerprint)
    return fingerprint


def diff_fingerprints(before: Fingerprint, after: Fingerprint) -> list[Violation]:
    """Every path whose dirty state differs between the two fingerprints."""

    violations: list[Violation] = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None and new is not None:
            violations.append(Violation("new", new[0], path))
        elif new is None:
            violations.append(Violation("restored", "--", path))
        else:
            violations.append(Violation("changed", new[0], path))
    return violations


def format_report(violations: list[Violation]) -> list[str]:
    """Report lines ready to print: one ``[TREE-HYGIENE]`` line per path."""

    lines = [violation.render() for violation in violations]
    lines.append(f"{REPORT_PREFIX} {FAILURE_MESSAGE}")
    return lines
