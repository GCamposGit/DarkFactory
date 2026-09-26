"""Deterministic test for DarkFac dependency contract and requirements integrity.

Ensures that:
1. All third-party packages imported in core/ and hub/ production modules are
   explicitly declared in requirements.txt (preventing missing dependencies on workers).
2. requirements.txt contains valid package specifications and no duplicates.
3. The remote worker requirements synchronizer logic behaves correctly on changes.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"

# Known package distribution names to their imported module names mapping
# (e.g. Pillow installs as PIL, PyYAML installs as yaml)
PACKAGE_TO_MODULE_MAP = {
    "pillow": "pil",
    "pyyaml": "yaml",
    "pytest-xdist": "xdist",
    "pytest-timeout": "pytest_timeout",
    "uvicorn[standard]": "uvicorn",
}

# Optional or isolated modules explicitly permitted without being in core requirements.txt
# (e.g. HF-02 isolated DBOS/Postgres spike, optional heavy local audio models)
ALLOWED_OPTIONAL_IMPORTS = {
    "dbos",            # isolated in spikes/runtime_choice
    "psycopg",         # isolated in spikes/runtime_choice
    "faster_whisper",  # optional local GPU transcription skill
    "torch",           # optional dependency of local whisper/audio
    "certifi",         # transitive dependency of httpx/requests
    "starlette",       # transitive dependency of fastapi
}


def _get_declared_requirements() -> dict[str, str]:
    """Parse requirements.txt into a dict of normalized_package_name -> raw_spec."""
    assert REQUIREMENTS_FILE.exists(), f"requirements.txt not found at {REQUIREMENTS_FILE}"
    declared: dict[str, str] = {}
    lines = REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
    for line in lines:
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        # Split package name before any version specifier
        pkg_name = cleaned.split(">")[0].split("<")[0].split("=")[0].split("~")[0].strip().lower()
        if pkg_name:
            declared[pkg_name] = cleaned
    return declared


def test_requirements_file_has_essential_packages() -> None:
    """requirements.txt must contain the essential deterministic dependencies."""
    declared = _get_declared_requirements()
    essential = [
        "fastapi",
        "uvicorn[standard]",
        "pydantic",
        "numpy",
        "soundfile",
        "pillow",
        "pytest",
        "pytest-xdist",
        "httpx",
        "cryptography",
        "pyyaml",
    ]
    missing = [pkg for pkg in essential if pkg not in declared]
    assert not missing, f"Missing essential packages in requirements.txt: {missing}"


def test_no_duplicate_requirements() -> None:
    """Ensure no duplicate package definitions exist in requirements.txt."""
    lines = [
        line.strip()
        for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    pkg_names = [line.split(">")[0].split("<")[0].split("=")[0].split("~")[0].strip().lower() for line in lines]
    duplicates = [pkg for pkg in set(pkg_names) if pkg_names.count(pkg) > 1]
    assert not duplicates, f"Duplicate entries found in requirements.txt: {duplicates}"


def test_production_imports_covered_by_requirements() -> None:
    """Every third-party package imported in core/ and hub/ must be in requirements.txt."""
    declared_pkgs = _get_declared_requirements()
    
    # Map declared packages to their imported module names
    declared_modules = set()
    for pkg in declared_pkgs:
        mapped = PACKAGE_TO_MODULE_MAP.get(pkg, pkg.replace("-", "_"))
        declared_modules.add(mapped)

    stdlib = set(sys.stdlib_module_names)
    internal_namespaces = {"core", "hub", "spikes", "tests"}

    imported_third_party: set[str] = set()

    for folder in [REPO_ROOT / "core", REPO_ROOT / "hub"]:
        for py_path in folder.rglob("*.py"):
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0].lower()
                        if top not in stdlib and top not in internal_namespaces:
                            imported_third_party.add(top)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:
                        top = node.module.split(".")[0].lower()
                        if top not in stdlib and top not in internal_namespaces:
                            imported_third_party.add(top)

    unaccounted = imported_third_party - declared_modules - ALLOWED_OPTIONAL_IMPORTS
    assert not unaccounted, (
        f"The following third-party module(s) are imported in core/ or hub/ but NOT declared in requirements.txt: "
        f"{sorted(unaccounted)}. Add them to requirements.txt so headless/remote test workers do not fail!"
    )


def test_worker_sync_requirements_skips_when_hash_matches(tmp_path: Path) -> None:
    """Remote worker _sync_requirements must skip pip install if hash hasn't changed."""
    from core.harness.remote_worker import JobManager, HarnessJob

    state_dir = tmp_path / "worker_state"
    state_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    req_file = worktree / "requirements.txt"
    req_content = b"fastapi>=0.115\npydantic>=2\n"
    req_file.write_bytes(req_content)

    content_hash = hashlib.sha256(req_content).hexdigest()
    hash_file = state_dir / ".requirements.sha256"
    hash_file.write_text(content_hash, encoding="utf-8")

    manager = MagicMock(spec=JobManager)
    manager.state_dir = state_dir

    job = HarnessJob(
        job_id="job-123",
        candidate_sha="sha1",
        tree_sha="tree1",
        quick=True,
        include_holdout=False,
        requesting_host="notebook",
        ref_name="refs/test",
    )

    with patch("subprocess.run") as mock_subp:
        JobManager._sync_requirements(manager, worktree, job)
        mock_subp.assert_not_called()


def test_worker_sync_requirements_installs_on_hash_mismatch(tmp_path: Path) -> None:
    """Remote worker _sync_requirements must run pip install when hash changes."""
    from core.harness.remote_worker import JobManager, HarnessJob

    state_dir = tmp_path / "worker_state"
    state_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    req_file = worktree / "requirements.txt"
    req_content = b"fastapi>=0.115\npydantic>=2\ncryptography>=42.0\n"
    req_file.write_bytes(req_content)

    content_hash = hashlib.sha256(req_content).hexdigest()
    hash_file = state_dir / ".requirements.sha256"
    hash_file.write_text("old_stale_hash", encoding="utf-8")

    manager = MagicMock(spec=JobManager)
    manager.state_dir = state_dir

    job = HarnessJob(
        job_id="job-456",
        candidate_sha="sha2",
        tree_sha="tree2",
        quick=True,
        include_holdout=False,
        requesting_host="notebook",
        ref_name="refs/test",
    )

    mock_res = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_res) as mock_subp:
        JobManager._sync_requirements(manager, worktree, job)
        assert mock_subp.called
        assert hash_file.read_text(encoding="utf-8").strip() == content_hash
