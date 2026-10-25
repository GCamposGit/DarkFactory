"""Environment builder for development-agent subprocesses (USR-172).

The agent process receives only what it needs (harness auth, PATH, ...). Deploy, backup, DB and
delivery credentials are removed; the delivery executor keeps its own environment untouched.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[2] / ".factory" / "config" / "agent_env_policy.json"

_FALLBACK: dict = {
    "keep_names": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY"],
    "allow_names": ["PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME",
                    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "USERNAME", "COMPUTERNAME", "OS", "LANG", "TERM"],
    "allow_patterns": ["^PROGRAMFILES", "^PROCESSOR_", "^LC_", "^PYTHON(?!STARTUP$)", "^(CLAUDE|ANTHROPIC|CODEX|GROK)_"],
    "deny_names": ["GH_TOKEN", "GITHUB_TOKEN", "DATABASE_URL"],
    "deny_patterns": ["^DOKPLOY", "BACKUP", "^GH_ADMIN", "^POSTGRES", "^TELEGRAM", "^GH_APP_", "^AWS_", "(PASSWORD|PASSWD)$", "_SECRET(_KEY)?$", "SIGNING_KEY", "^SOPS_", "^AGE_"],
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


_ALWAYS_DENY = re.compile(r"^(SOPS_|AGE_|GH_APP_)", re.IGNORECASE)


def build_agent_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    capabilities: Iterable[str] = (),
    policy: Optional[Mapping] = None,
) -> dict[str, str]:
    """Return the ALLOWLISTED subset of `base` (default os.environ) (USR-343 S6).

    Kept: `keep_names` (harness auth), `allow_names`/`allow_patterns` (OS + non-secret runtime) unless a
    deny rule matches, and ticket `capabilities` (explicit, per ticket). SOPS_/AGE_/GH_APP_ are never
    passed, not even via capabilities. Everything else is dropped (names logged at debug only).
    """
    pol = policy if policy is not None else load_policy()
    keep = {n.upper() for n in pol.get("keep_names", [])}
    caps = {str(c).upper() for c in capabilities}
    allow = {n.upper() for n in pol.get("allow_names", [])}
    allow_pat = [re.compile(p, re.IGNORECASE) for p in pol.get("allow_patterns", [])]
    deny = {n.upper() for n in pol.get("deny_names", [])}
    deny_pat = [re.compile(p, re.IGNORECASE) for p in pol.get("deny_patterns", [])]
    src = os.environ if base is None else base
    out: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in src.items():
        upper = name.upper()
        if _ALWAYS_DENY.search(name):
            ok = False
        elif upper in keep or upper in caps:
            ok = True
        elif upper in deny or any(p.search(name) for p in deny_pat):
            ok = False
        else:
            ok = upper in allow or any(p.search(name) for p in allow_pat)
        if ok:
            out[name] = value
        else:
            dropped.append(name)
    if dropped:
        logger.debug("agent env dropped %d variable names: %s", len(dropped), sorted(dropped))
    return out
