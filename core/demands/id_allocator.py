"""Reserve demand IDs across processes and worktrees of one Git repository."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Iterator


LEDGER = ".factory/demands/demands.json"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except FileNotFoundError as exc:
        # No git binary (e.g. the slim DarkHub container): behave like "not a repository".
        return subprocess.CompletedProcess(["git", *args], 127, "", str(exc))


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """An OS lock, released even if allocation fails or the process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out reserving demand ID at {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _numbers(raw: str, prefix: str) -> list[int]:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload["demands"]
    if not isinstance(payload, list):
        raise ValueError("Demand ledger must contain a list")
    accepted = f"{re.escape(prefix)}|DF" if prefix == "USR" else re.escape(prefix)
    pattern = re.compile(rf"^(?:{accepted})-(\d+)$")
    numbers: list[int] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Demand ledger contains a non-object row")
        match = pattern.fullmatch(str(item.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def title_collisions(base_raw: str, candidate_raw: str) -> list[str]:
    """Describe IDs that would silently overwrite a different demand on merge."""
    def titles(raw: str) -> dict[str, str]:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload["demands"]
        if not isinstance(payload, list):
            raise ValueError("Demand ledger must contain a list")
        return {str(row["id"]): str(row["title"]) for row in payload}

    base = titles(base_raw)
    candidate = titles(candidate_raw)
    return [
        f"{ticket_id}: origin/main={base[ticket_id]!r}, branch={title!r}"
        for ticket_id, title in candidate.items()
        if ticket_id in base and base[ticket_id] != title
    ]


def _worktree_paths(root: Path) -> list[Path]:
    result = _git(root, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise RuntimeError(f"Cannot list Git worktrees: {result.stderr.strip()}")
    return [Path(line[9:]) for line in result.stdout.splitlines() if line.startswith("worktree ")]


def reserve_ticket_id(path: Path, prefix: str) -> str:
    """Reserve the next ID using one counter in the common Git directory.

    For an isolated non-Git ledger, keep the same reservation algorithm beside
    that ledger. Git failures other than absence of a repository fail closed.
    """
    path = path.resolve()
    root = path.parent
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    in_git = common.returncode == 0
    if in_git:
        common_dir = Path(common.stdout.strip()).resolve()
    else:
        common_dir = path.parent
    lock_path = common_dir / "darkfac-ids.lock"
    counter_path = common_dir / "darkfac-ids.json"
    with _file_lock(lock_path):
        numbers: list[int] = []
        if path.is_file():
            numbers.extend(_numbers(path.read_text(encoding="utf-8"), prefix))
        if in_git:
            worktrees = _worktree_paths(root)
            for worktree in worktrees:
                ledger = worktree / LEDGER
                if ledger.is_file():
                    numbers.extend(_numbers(ledger.read_text(encoding="utf-8"), prefix))
            main = _git(root, "show", f"origin/main:{LEDGER}")
            if main.returncode == 0:
                numbers.extend(_numbers(main.stdout, prefix))
            refs = _git(root, "for-each-ref", "--format=%(refname)",
                        "refs/heads/ticket/", "refs/remotes/origin/ticket/")
            if refs.returncode != 0:
                raise RuntimeError(f"Cannot list ticket branches: {refs.stderr.strip()}")
            for ref in refs.stdout.splitlines():
                shown = _git(root, "show", f"{ref}:{LEDGER}")
                if shown.returncode == 0:
                    numbers.extend(_numbers(shown.stdout, prefix))
        counters = json.loads(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else {}
        next_number = max([*numbers, int(counters.get(prefix, 0))], default=0) + 1
        counters[prefix] = next_number
        temporary = counter_path.with_name(f"{counter_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(counters, sort_keys=True), encoding="utf-8")
        temporary.replace(counter_path)
        return f"{prefix}-{next_number:02d}"
