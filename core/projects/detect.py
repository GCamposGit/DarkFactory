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


def detect_commands(repo_dir: Path) -> ProjectCommands:
    """Inspect a repository checkout and return the commands DarkFac can run.

    Detection order:
    1. `harness.config.json` present (DarkFac-adopted project) -> harness quick validate.
    2. `package.json` present -> Node setup/test/build, package manager picked from lockfile.
    3. `pyproject.toml` or `requirements.txt` present -> Python pip install + pytest.
    4. Nothing recognized (including an empty repo) -> all-empty `ProjectCommands()`.
    """
    repo_dir = Path(repo_dir)

    if (repo_dir / "harness.config.json").is_file():
        return ProjectCommands(validate=list(_HARNESS_QUICK_VALIDATE))

    package_json = repo_dir / "package.json"
    if package_json.is_file():
        return _detect_node_commands(repo_dir, package_json)

    if (repo_dir / "pyproject.toml").is_file() or (repo_dir / "requirements.txt").is_file():
        return _detect_python_commands(repo_dir)

    return ProjectCommands()


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


def _detect_python_commands(repo_dir: Path) -> ProjectCommands:
    setup: list[str] = []
    if (repo_dir / "requirements.txt").is_file():
        setup.append("python -m pip install -r requirements.txt")
    elif (repo_dir / "pyproject.toml").is_file():
        setup.append("python -m pip install -e .")

    has_tests = (
        (repo_dir / "tests").is_dir()
        or any(repo_dir.glob("test_*.py"))
        or any(repo_dir.glob("*_test.py"))
    )
    validate = ["python -m pytest -q"] if has_tests else []

    return ProjectCommands(setup=setup, validate=validate, build=[])
