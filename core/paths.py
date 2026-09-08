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
