"""Model lifecycle CLI (USR-185 slice 7): promote, rollback, retire-due. Audited in .factory/models/lifecycle_ledger.jsonl.

Usage:
  python scripts/model_lifecycle.py promote <model_id> [--role ROLE]
  python scripts/model_lifecycle.py rollback <role|model_id> --reason gate_failure|cost|quota
  python scripts/model_lifecycle.py retire-due
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.models.lifecycle import ROLLBACK_REASONS, LifecycleError, promote, retire_due, rollback  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default="factory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote")
    p.add_argument("model_id")
    p.add_argument("--role", default=None)
    r = sub.add_parser("rollback")
    r.add_argument("target")
    r.add_argument("--reason", required=True, choices=ROLLBACK_REASONS)
    sub.add_parser("retire-due")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        if args.cmd == "promote":
            out: object = promote(args.model_id, now=now, role=args.role, actor=args.actor, docs_root=REPO_ROOT)
        elif args.cmd == "rollback":
            out = rollback(args.target, args.reason, now=now, actor=args.actor, docs_root=REPO_ROOT)
        else:
            out = retire_due(now.date(), now=now, actor=args.actor, docs_root=REPO_ROOT)
    except LifecycleError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
