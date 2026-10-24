"""Environment builder for development-agent subprocesses (USR-172).

The agent process receives only what it needs (harness auth, PATH, ...). Deploy, backup, DB and
delivery credentials are removed; the delivery executor keeps its own environment untouched.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Optional

POLICY_PATH = Path(__file__).resolve().parents[2] / ".factory" / "config" / "agent_env_policy.json"

_FALLBACK: dict = {
    "keep_names": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY"],
    "deny_names": ["GH_TOKEN", "GITHUB_TOKEN", "DATABASE_URL"],
    "deny_patterns": ["^DOKPLOY", "BACKUP", "^GH_ADMIN", "^POSTGRES", "^TELEGRAM", "^AWS_", "(PASSWORD|PASSWD)$", "_SECRET(_KEY)?$"],
}


def load_policy(path: Optional[Path] = None) -> dict:
    """Load the policy; fail closed to the built-in denylist if the file is missing or invalid."""
    try:
        data = json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("deny_patterns"), list):
            return data
    except (OSError, ValueError):
        pass
    return dict(_FALLBACK)


def build_agent_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    capabilities: Iterable[str] = (),
    policy: Optional[Mapping] = None,
) -> dict[str, str]:
    """Return a copy of `base` (default os.environ) without other roles' secrets.

    `capabilities` are ticket-granted env names that bypass the denylist (explicit, per ticket).
    """
    pol = policy if policy is not None else load_policy()
    keep = {n.upper() for n in pol.get("keep_names", [])} | {str(c).upper() for c in capabilities}
    deny = {n.upper() for n in pol.get("deny_names", [])}
    patterns = [re.compile(p, re.IGNORECASE) for p in pol.get("deny_patterns", [])]
    src = os.environ if base is None else base
    out: dict[str, str] = {}
    for name, value in src.items():
        upper = name.upper()
        if upper not in keep and (upper in deny or any(p.search(name) for p in patterns)):
            continue
        out[name] = value
    return out
