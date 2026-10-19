#!/usr/bin/env python3
"""Backfill the cross-project catalog from recent deliveries (USR-220).

Reads ``git log`` of the main branch (commits whose subject has ``[USR-NNN]``) and the
demands ledger, then registers reusable components / evolutions with the same heuristic
used at delivery time (``core.catalog.delivery``). Idempotent: ids are stable.

    python C:\\dev\\DarkFac\\scripts\\backfill_catalog.py --dry-run          # only list
    python C:\\dev\\DarkFac\\scripts\\backfill_catalog.py --days 30          # register
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.catalog.delivery import build_payload, classify_changes, push_to_hub, register_delivery  # noqa: E402
from core.git.safe_show import safe_show  # noqa: E402

TICKET_RE = re.compile(r"\[(USR-\d+)\]")
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
LEDGER = Path(".factory") / "demands" / "demands.json"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
    )
    return proc.stdout if proc.returncode == 0 else ""


def load_ledger_tags(root: Path) -> dict[str, list[str]]:
    path = root / LEDGER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("tickets", data) if isinstance(data, dict) else data
    out: dict[str, list[str]] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out[str(item["id"])] = [str(t) for t in item.get("tags") or []]
    return out


def repo_url(root: Path) -> str:
    url = _git(root, "remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return f"https://github.com/{m.group(1)}" if m else ""


def delivery_commits(root: Path, branch: str, days: int) -> list[tuple[str, str, str]]:
    raw = _git(root, "log", branch, f"--since={days}.days", "--no-merges", "--format=%H%x1f%s")
    commits = []
    for line in raw.splitlines():
        sha, _, subject = line.partition("\x1f")
        if sha and TICKET_RE.search(subject):
            commits.append((sha, subject, TICKET_RE.search(subject).group(1)))  # type: ignore[union-attr]
    return list(reversed(commits))  # oldest first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--branch", default="origin/main")
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument("--push", action="store_true", help="also send the result to the production Hub (ignored with --dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be registered; write nothing")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root: Path = args.root
    tags = load_ledger_tags(root)
    base_url = repo_url(root)
    pushed: dict[str, list] = {"components": [], "evolutions": []}
    total_c: set[str] = set()
    total_e: set[str] = set()
    commits = delivery_commits(root, args.branch, args.days)
    for sha, subject, ticket_id in commits:
        raw = _git(root, "diff-tree", "--no-commit-id", "--name-status", "-r", "--no-renames", sha)
        changes = [(p[0], p[-1]) for p in (ln.split("\t") for ln in raw.splitlines()) if len(p) >= 2]
        pr = PR_RE.search(subject)
        pr_url = f"{base_url}/pull/{pr.group(1)}" if pr and base_url else ""

        def read(path: str, _sha: str = sha) -> str | None:
            try:
                out = safe_show(_sha, path, cwd=root)
            except (ValueError, OSError, subprocess.SubprocessError):
                return None
            return out.stdout if out.returncode == 0 else None

        title = TICKET_RE.sub("", subject).split(":", 1)[-1].strip()
        title = PR_RE.sub("", title).strip()
        res = register_delivery(ticket_id, title, changes, read, tags.get(ticket_id, []), pr_url, dry_run=args.dry_run)
        if args.push and not args.dry_run:
            comps, evos = classify_changes(ticket_id, title, changes, read, tags.get(ticket_id, []), pr_url)
            payload = build_payload(comps, evos)
            pushed["components"] += payload["components"]
            pushed["evolutions"] += payload["evolutions"]
        total_c.update(res["components"])
        total_e.update(res["evolutions"])
        if res["components"] or res["evolutions"]:
            print(f"{ticket_id} {sha[:8]}: components={res['components']} evolutions={res['evolutions']}")
    if args.push and not args.dry_run:
        sent = True
        for i in range(0, max(len(pushed["components"]), len(pushed["evolutions"])), 100):
            chunk = {"components": pushed["components"][i:i + 100], "evolutions": pushed["evolutions"][i:i + 100]}
            sent = push_to_hub(chunk, repo_root=root, timeout=30.0) and sent
        print(f"[PUSH] {'ok' if sent else 'failed or no key configured'}")
    mode = "DRY-RUN" if args.dry_run else "REGISTERED"
    print(f"[{mode}] commits={len(commits)} components={len(total_c)} evolutions={len(total_e)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
