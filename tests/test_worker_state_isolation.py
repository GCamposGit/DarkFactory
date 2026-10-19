"""The suite never sees the host's real worker state dir.

On the grokbox the official gate runs INSIDE a job of the real worker. A test that
built the worker app without a state dir recovered that live job (its owner looked
dead: psutil start times drift after a VM freeze/restore) and SIGTERMed the job's own
process tree ~2 s into the parallel step. ``tests/conftest.py`` isolates the dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from core.harness import remote_worker


def test_worker_state_dir_is_an_isolated_temp_dir() -> None:
    state_dir = remote_worker.default_worker_state_dir().resolve()

    assert os.environ.get(remote_worker.ENV_WORKER_STATE_DIR)
    assert state_dir.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not state_dir.is_relative_to((Path.home() / ".local" / "state").resolve())
