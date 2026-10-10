"""Reserve demand IDs across processes, worktrees, and origin of one Git repository."""

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
_MAX_ID_RESERVATION_ATTEMPTS = 5


def _git(
    root: Path, *args: str, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False, env=env,
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


def _id_pattern(prefix: str) -> re.Pattern[str]:
    accepted = f"{re.escape(prefix)}|DF" if prefix == "USR" else re.escape(prefix)
    return re.compile(rf"^(?:{accepted})-(\d+)$")


def _ledger_rows(raw: str) -> list[dict[str, object]]:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload["demands"]
    if not isinstance(payload, list):
        raise ValueError("Demand ledger must contain a list")
    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Demand ledger contains a non-object row")
        rows.append(item)
    return rows


def _numbers(raw: str, prefix: str) -> list[int]:
    pattern = _id_pattern(prefix)
    numbers: list[int] = []
    for item in _ledger_rows(raw):
        match = pattern.fullmatch(str(item.get("id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def _matching_ids(raw: str, prefix: str) -> set[str]:
    pattern = _id_pattern(prefix)
    found: set[str] = set()
    for item in _ledger_rows(raw):
        ticket_id = str(item.get("id", ""))
        if pattern.fullmatch(ticket_id):
            found.add(ticket_id)
    return found


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


def _show_ledger(root: Path, ref: str) -> str | None:
    shown = _git(root, "show", f"{ref}:{LEDGER}")
    if shown.returncode != 0:
        return None
    return shown.stdout


def _origin_remote_configured(root: Path) -> bool:
    remotes = _git(root, "remote")
    if remotes.returncode != 0:
        raise RuntimeError(f"Cannot list Git remotes: {remotes.stderr.strip()}")
    return any(line.strip() == "origin" for line in remotes.stdout.splitlines())


def _fetch_origin_refs(root: Path) -> None:
    """Refresh origin/main and origin/ticket/* before an ID is chosen.

    No origin remote means a local-only repository (the slim DarkHub image has
    none). A configured origin that cannot be fetched fails closed: reserving
    from a stale remote-tracking ref is what collided USR-187.
    """
    if not _origin_remote_configured(root):
        return
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    try:
        fetched = _git(
            root,
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
            "+refs/heads/ticket/*:refs/remotes/origin/ticket/*",
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Cannot fetch origin/main and ticket refs before reserving a demand ID: timed out"
        ) from exc
    if fetched.returncode != 0:
        detail = fetched.stderr.strip() or fetched.stdout.strip() or f"exit {fetched.returncode}"
        raise RuntimeError(
            "Cannot fetch origin/main and ticket refs before reserving a demand ID: " + detail
        )


def _occupied_numbers(root: Path, path: Path, prefix: str, *, in_git: bool) -> list[int]:
    numbers: list[int] = []
    if path.is_file():
        numbers.extend(_numbers(path.read_text(encoding="utf-8"), prefix))
    if not in_git:
        return numbers
    for worktree in _worktree_paths(root):
        ledger = worktree / LEDGER
        if ledger.is_file():
            numbers.extend(_numbers(ledger.read_text(encoding="utf-8"), prefix))
    main_raw = _show_ledger(root, "origin/main")
    if main_raw is not None:
        numbers.extend(_numbers(main_raw, prefix))
    refs = _git(
        root, "for-each-ref", "--format=%(refname)",
        "refs/heads/ticket/", "refs/remotes/origin/ticket/",
    )
    if refs.returncode != 0:
        raise RuntimeError(f"Cannot list ticket branches: {refs.stderr.strip()}")
    for ref in refs.stdout.splitlines():
        ref = ref.strip()
        if not ref:
            continue
        shown = _show_ledger(root, ref)
        if shown is not None:
            numbers.extend(_numbers(shown, prefix))
    return numbers


def _origin_contains_id(root: Path, candidate: str, prefix: str) -> bool:
    """True when origin/main already registered this ID or the same numeric slot."""
    raw = _show_ledger(root, "origin/main")
    if raw is None:
        return False
    if candidate in _matching_ids(raw, prefix):
        return True
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", candidate)
    if match is None:
        return False
    return int(match.group(1)) in _numbers(raw, prefix)


def _write_counter(counter_path: Path, counters: dict[str, object]) -> None:
    temporary = counter_path.with_name(f"{counter_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(counters, sort_keys=True), encoding="utf-8")
    temporary.replace(counter_path)


def reserve_ticket_id(path: Path, prefix: str) -> str:
    """Reserve the next ID using one counter in the common Git directory.

    For an isolated non-Git ledger, keep the same reservation algorithm beside
    that ledger. When an origin remote exists, fetch origin/main and ticket refs
    before choosing. A candidate that already exists on origin/main after that
    fetch is rejected and the reservation is recalculated. Git failures other
    than absence of a repository fail closed.
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
        counters = json.loads(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else {}
        floor = int(counters.get(prefix, 0))
        candidate = f"{prefix}-?"
        for _attempt in range(_MAX_ID_RESERVATION_ATTEMPTS):
            if in_git:
                _fetch_origin_refs(root)
            numbers = _occupied_numbers(root, path, prefix, in_git=in_git)
            next_number = max([*numbers, floor], default=0) + 1
            candidate = f"{prefix}-{next_number:02d}"
            if in_git and _origin_contains_id(root, candidate, prefix):
                # Already on origin/main. Do not return it and do not merge a duplicate.
                floor = max(floor, next_number)
                continue
            counters[prefix] = next_number
            _write_counter(counter_path, counters)
            return candidate
        raise RuntimeError(
            f"Refusing to reserve demand ID {candidate}: it already exists on origin/main after fetch"
        )
