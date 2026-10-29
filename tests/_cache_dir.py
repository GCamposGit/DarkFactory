"""Keep the pytest cache outside the checkout (USR-243).

``_pytest.cacheprovider`` builds ``.pytest_cache`` through ``tempfile.mkdtemp``.
On Windows, Python 3.12 gives such a directory a protected, non-inheriting ACL
(SYSTEM, Administrators and the creating owner only). Another principal, such
as a sandboxed agent user or the trash cleaner, then gets WinError 5 on
``git worktree remove`` / ``rmtree``. Keep ``cache_dir`` in a per-rootdir
folder under the system temporary directory: ``--lf`` works across runs and
the cache remains outside the worktree, while sandboxed runs can write it.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any


def external_cache_dir(rootpath: Path) -> Path:
    """Return the stable out-of-tree cache directory for ``rootpath``."""

    base = Path(tempfile.gettempdir())
    key = hashlib.sha256(str(Path(rootpath).resolve()).lower().encode("utf-8"))
    return base / "DarkFac" / "pytest-cache" / key.hexdigest()[:16]


def redirect_cache_dir(config: Any) -> Path | None:
    """Point ``cache_dir`` at the external folder unless explicitly overridden."""

    overrides = config.getoption("override_ini", default=None) or []
    if any(str(item).split("=", 1)[0].strip() == "cache_dir" for item in overrides):
        return None
    target = external_cache_dir(Path(config.rootpath))
    inicache = getattr(config, "_inicache", None)
    if not isinstance(inicache, dict):
        return None
    inicache["cache_dir"] = str(target)
    return target
