"""Pure, offline command autodetection for adopted project repositories.

Used by `core.projects.registry.resolve_commands` to fill in setup/validate/
build/smoke commands that a project's registry entry left empty. Never
touches the network and never executes any command itself — it only reads a
handful of well-known manifest files and returns the commands DarkFac should
run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import ProjectCommands

logger = logging.getLogger(__name__)

_HARNESS_QUICK_VALIDATE = ["python core/harness/runner.py --quick"]
_HARNESS_RUNNER = Path("core") / "harness" / "runner.py"


def detect_commands(repo_dir: Path) -> ProjectCommands:
    """Inspect a repository checkout and return the commands DarkFac can run.

    Detection order:
    1. `harness.config.json` present -> Python pip install setup (if
       requirements.txt/pyproject.toml present) plus:
       - the DarkFac repo itself (`core/harness/runner.py` exists): the official
         `python core/harness/runner.py --quick`;
       - any other project carrying a harness config (e.g. an adopted repo with only
         the `.factory/darkfac.py` runtime, no runner.py): the `cmd` of each
         `quick: true` step of that config, in order. The runner would not exist
         there, and the official runners refuse the dirty tree the development
         stage validates.
       A harness config yielding no usable quick step falls through to 2/3.
    2. `package.json` present -> Node setup/test/build, package manager picked from lockfile.
    3. `pyproject.toml` or `requirements.txt` present -> Python pip install + pytest.
    4. Nothing recognized (including an empty repo) -> all-empty `ProjectCommands()`.
    """
    repo_dir = Path(repo_dir)

    harness_config = repo_dir / "harness.config.json"
    if harness_config.is_file():
        # A fresh clone still needs its Python deps installed before the tests can import anything.
        setup = _detect_python_setup(repo_dir)
        if (repo_dir / _HARNESS_RUNNER).is_file():
            return ProjectCommands(setup=setup, validate=list(_HARNESS_QUICK_VALIDATE))
        quick_cmds = _harness_quick_commands(harness_config)
        if quick_cmds:
            return ProjectCommands(setup=setup, validate=quick_cmds)

    package_json = repo_dir / "package.json"
    if package_json.is_file():
        return _detect_node_commands(repo_dir, package_json)

    if (repo_dir / "pyproject.toml").is_file() or (repo_dir / "requirements.txt").is_file():
        return _detect_python_commands(repo_dir)

    return ProjectCommands()


def _harness_quick_commands(harness_config: Path) -> list[str]:
    """`cmd` of every `quick: true` (non-holdout) step of a harness config, in order; [] if unusable."""
    try:
        data = json.loads(harness_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Failed to parse %s: %s", harness_config, exc)
        return []
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    commands: list[str] = []
    for step in steps:
        if not isinstance(step, dict) or not step.get("quick") or step.get("holdout"):
            continue
        cmd = step.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            commands.append(cmd.strip())
    return commands


def _detect_node_commands(repo_dir: Path, package_json: Path) -> ProjectCommands:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Failed to parse %s: %s", package_json, exc)
        return ProjectCommands()

    if (repo_dir / "pnpm-lock.yaml").is_file():
        package_manager = "pnpm"
        setup = ["pnpm install --frozen-lockfile"]
    elif (repo_dir / "yarn.lock").is_file():
        package_manager = "yarn"
        setup = ["yarn install --frozen-lockfile"]
    else:
        package_manager = "npm"
        setup = ["npm ci"]

    scripts = data.get("scripts") if isinstance(data, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}

    validate: list[str] = []
    if "test" in scripts:
        validate.append("npm test" if package_manager == "npm" else f"{package_manager} test")

    build: list[str] = []
    if "build" in scripts:
        build.append("npm run build" if package_manager == "npm" else f"{package_manager} run build")

    return ProjectCommands(setup=setup, validate=validate, build=build)


def _detect_python_setup(repo_dir: Path) -> list[str]:
    """Return the pip install command for a Python repo, or [] if neither
    requirements.txt nor pyproject.toml is present."""
    if (repo_dir / "requirements.txt").is_file():
        return ["python -m pip install -r requirements.txt"]
    if (repo_dir / "pyproject.toml").is_file():
        return ["python -m pip install -e ."]
    return []


def _detect_python_commands(repo_dir: Path) -> ProjectCommands:
    setup = _detect_python_setup(repo_dir)

    has_tests = (
        (repo_dir / "tests").is_dir()
        or any(repo_dir.glob("test_*.py"))
        or any(repo_dir.glob("*_test.py"))
    )
    validate = ["python -m pytest -q"] if has_tests else []

    return ProjectCommands(setup=setup, validate=validate, build=[])
