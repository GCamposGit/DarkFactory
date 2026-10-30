"""Keep the pytest cache outside the checkout (USR-243, USR-421).

``_pytest.cacheprovider`` builds the cache directory from ``Cache.set`` during
``pytest_sessionfinish`` through ``tempfile.mkdtemp``. On Windows that call
stays blocked when the destination cannot be written (observed for
``%LOCALAPPDATA%``), so the process sits at 100% with no summary and no exit
code. Python 3.12 also gives ``.pytest_cache`` a protected, non-inheriting ACL
(SYSTEM, Administrators and the creating owner only). Another principal, such
as a sandboxed agent user or the trash cleaner, then gets WinError 5 on
``git worktree remove`` / ``rmtree``. Keep ``cache_dir`` in a per-rootdir
folder under the system temporary directory: ``--lf`` works across runs and
the cache remains outside the worktree, while sandboxed runs can write it.

The path is stored before the cache plugin reads ``cache_dir`` and applied
again on the live ``Cache`` at the start of ``pytest_sessionfinish``, before
``Cache.set`` creates the directory.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

_SESSIONFINISH_PLUGIN = "darkfac_pytest_cache_dir"


def external_cache_dir(rootpath: Path) -> Path:
    """Return the stable out-of-tree cache directory for ``rootpath``."""

    base = Path(tempfile.gettempdir())
    key = hashlib.sha256(str(Path(rootpath).resolve()).lower().encode("utf-8"))
    return base / "DarkFac" / "pytest-cache" / key.hexdigest()[:16]


def _retarget_live_cache(config: Any, target: Path) -> None:
    cache = getattr(config, "cache", None)
    if cache is not None and hasattr(cache, "_cachedir"):
        cache._cachedir = target


def _bind_sessionfinish(config: Any) -> None:
    """Reapply the external directory before cacheprovider writes it.

    ``pytest_configure`` of this plugin runs first and fills ``_inicache``, but
    ``Cache._cachedir`` is fixed when the cache plugin is configured. A later
    ``pytest_sessionfinish`` (tryfirst) runs before ``Cache.set`` and points the
    live object at the same folder, which is the call that used to block.
    """

    manager = getattr(config, "pluginmanager", None)
    if manager is None:
        return
    get_plugin = getattr(manager, "get_plugin", None)
    if callable(get_plugin) and get_plugin(_SESSIONFINISH_PLUGIN) is not None:
        return
    register = getattr(manager, "register", None)
    if not callable(register):
        return

    import pytest

    class _CacheDirSessionFinish:
        @pytest.hookimpl(tryfirst=True)
        def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
            redirect_cache_dir(session.config)

    register(_CacheDirSessionFinish(), _SESSIONFINISH_PLUGIN)


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
    _retarget_live_cache(config, target)
    _bind_sessionfinish(config)
    return target
