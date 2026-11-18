"""Runtime sidecars that must not become product artifacts.

These snapshots are recomputable operational evidence.  They are always
written below STATE_ROOT/runs and replaced atomically so readers never observe
a partially-written JSON document.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

FLEET_POLICY_SNAPSHOT = "fleet_policy_snapshot.json"
MODEL_AUDIT_RECEIPT = "model_audit_receipt.json"

_RUNTIME_SIDECARS = frozenset(
    {
        FLEET_POLICY_SNAPSHOT,
        MODEL_AUDIT_RECEIPT,
    }
)


def resolve_state_root(
    state_root: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the persistent runtime state root.

    Explicit configuration wins.  PROJECT_ROOT is used only to provide the
    backwards-compatible ``.factory`` default; interactive harness preferences
    never participate in this decision.
    """

    if state_root is not None:
        return Path(state_root).expanduser().resolve()

    configured = os.environ.get("STATE_ROOT") or os.environ.get(
        "DARKFAC_STATE_ROOT"
    )
    if configured:
        return Path(configured).expanduser().resolve()

    root_value = project_root or os.environ.get("PROJECT_ROOT") or Path.cwd()
    return (Path(root_value).expanduser().resolve() / ".factory")


def runtime_runs_dir(
    state_root: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    return resolve_state_root(state_root, project_root=project_root) / "runs"


def runtime_sidecar_path(
    filename: str,
    *,
    state_root: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    if filename not in _RUNTIME_SIDECARS:
        raise ValueError(f"unsupported runtime sidecar: {filename!r}")
    return runtime_runs_dir(
        state_root,
        project_root=project_root,
    ) / filename


def atomic_write_json(path: Path, payload: Any) -> Path:
    """Serialize JSON through a same-directory temporary and atomic replace."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)

        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        return path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_fleet_policy_snapshot(
    snapshot: Mapping[str, Any],
    *,
    state_root: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    return atomic_write_json(
        runtime_sidecar_path(
            FLEET_POLICY_SNAPSHOT,
            state_root=state_root,
            project_root=project_root,
        ),
        snapshot,
    )


def write_model_audit_receipt(
    receipt: Mapping[str, Any],
    *,
    state_root: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> Path:
    return atomic_write_json(
        runtime_sidecar_path(
            MODEL_AUDIT_RECEIPT,
            state_root=state_root,
            project_root=project_root,
        ),
        receipt,
    )
