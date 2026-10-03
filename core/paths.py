"""Stable filesystem roots for source and embedded Dark Factory runtimes."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Return the consumer project root, or this repository when run from source."""

    configured = os.environ.get("DARKFAC_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def state_root() -> Path:
    """Return the root directory for mutable factory runtime state.

    Respects DARKFAC_STATE_ROOT if set in the environment,
    otherwise defaults to project_root() / ".factory".
    """
    configured = os.environ.get("DARKFAC_STATE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return project_root() / ".factory"
