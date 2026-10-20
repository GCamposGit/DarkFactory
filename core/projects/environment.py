"""Isolated execution environment for stand-alone products (USR-275).

The factory must not leak its own Python, PATH entries, PYTHONPATH, DARKFAC_*
variables or secrets into a product. `build_product_env` builds a minimal
allowlisted environment; `run_in_environment` runs commands with it and masks
any allowlisted secret value in the captured output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional

from core.line.stage_build import CommandRunResult, run_shell_commands
from core.projects.models import EnvironmentKind, ProjectEnvironment

# Host variables a product legitimately needs to run tools. Everything else is dropped.
BASE_ENV_ALLOWLIST: tuple[str, ...] = (
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LC_ALL",
    "LC_CTYPE", "TERM", "TZ", "CI",
)

_MASK = "***"
_MIN_MASKED_LEN = 4


class ProjectEnvironmentError(RuntimeError):
    """Structured failure: the declared environment is unusable."""

    def __init__(self, code: str, message: str, **details: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = dict(details)

    def as_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, "details": self.details}


def venv_dir(env: ProjectEnvironment, cwd: Path) -> Path:
    """Resolve the venv directory, which must stay inside the product checkout."""
    root = Path(cwd).resolve()
    target = (root / env.path).resolve()
    if target != root and root not in target.parents:
        raise ProjectEnvironmentError(
            "environment_path_outside_project", "environment.path must stay inside the product checkout",
            path=env.path,
        )
    return target


def venv_bin_dir(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


def _factory_path_entries(host_path: str) -> list[str]:
    """PATH entries kept: everything except the core's virtualenv locations."""
    blocked: list[Path] = []
    core_venv = os.environ.get("VIRTUAL_ENV")
    if core_venv:
        blocked.append(Path(core_venv).resolve())
    if sys.prefix != sys.base_prefix:
        blocked.append(Path(sys.prefix).resolve())
    kept: list[str] = []
    for entry in host_path.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            kept.append(entry)
            continue
        if any(resolved == b or b in resolved.parents for b in blocked):
            continue
        kept.append(entry)
    return kept


def build_product_env(
    env: ProjectEnvironment,
    cwd: Path,
    *,
    host_env: Optional[Mapping[str, str]] = None,
    require_ready: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Return `(child_env, secret_values_to_mask)` for the product."""
    host = dict(os.environ if host_env is None else host_env)
    upper_host = {k.upper(): k for k in host}
    child: dict[str, str] = {}
    for name in BASE_ENV_ALLOWLIST:
        key = upper_host.get(name.upper())
        if key is not None:
            child[key] = host[key]
    path_key = upper_host.get("PATH")
    entries = _factory_path_entries(host[path_key]) if path_key else []
    secrets: list[str] = []
    for name in env.env_allow:
        key = upper_host.get(name.upper())
        if key is not None and host[key]:
            child[key] = host[key]
            if len(host[key]) >= _MIN_MASKED_LEN:
                secrets.append(host[key])
    if env.kind is EnvironmentKind.VENV:
        venv = venv_dir(env, cwd)
        bin_dir = venv_bin_dir(venv)
        if require_ready and not bin_dir.is_dir():
            raise ProjectEnvironmentError(
                "environment_missing",
                f"declared venv not found; run the environment setup first: {venv}",
                kind=env.kind.value, path=str(venv),
            )
        entries.insert(0, str(bin_dir))
        child["VIRTUAL_ENV"] = str(venv)
    child["PATH"] = os.pathsep.join(entries)
    child["PYTHONNOUSERSITE"] = "1"
    return child, secrets


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    for value in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(value, _MASK)
    return text


def run_in_environment(
    env: ProjectEnvironment,
    commands: Iterable[str],
    cwd: Path,
    *,
    timeout_s: int = 1800,
    phase: str = "run",
    host_env: Optional[Mapping[str, str]] = None,
) -> CommandRunResult:
    """Run `commands` inside the product environment (`phase="setup"` tolerates a missing venv)."""
    commands = list(commands)
    if not commands:
        return CommandRunResult(ok=True, combined_output="", exit_code=0, duration_s=0.0)
    child, secrets = build_product_env(env, cwd, host_env=host_env, require_ready=phase != "setup")
    result = run_shell_commands(commands, cwd, timeout_s=timeout_s, env=child)
    if secrets:
        result = result.model_copy(update={"combined_output": mask_secrets(result.combined_output, secrets)})
    return result


def setup_environment(
    env: ProjectEnvironment,
    cwd: Path,
    extra_setup: Iterable[str] = (),
    *,
    timeout_s: int = 1800,
    host_env: Optional[Mapping[str, str]] = None,
) -> CommandRunResult:
    """Run `environment.setup` followed by the project's own `commands.setup`."""
    return run_in_environment(
        env, [*env.setup, *extra_setup], cwd, timeout_s=timeout_s, phase="setup", host_env=host_env
    )
