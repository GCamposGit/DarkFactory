"""Transactional, idempotent adoption of repositories by the Dark Factory.

# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260907_project_adoption_gateway
# Audit Doc: .factory/research/20260907_project_adoption_gateway/INSIGHTS.md
# Canonical Sources: .factory/research/20260907_project_adoption_gateway/ledger.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from .models import (
    AdoptionLock,
    AdoptionPlan,
    AdoptionResult,
    FileAction,
    GitSnapshot,
    PlannedFile,
    ProjectInspection,
    ProjectKind,
    StackProfile,
    TaskPreparation,
    VerificationReport,
)

LOCK_PATH = Path(".factory/darkfac.lock.json")
RUNTIME_ROOT = Path(".factory/runtime")
AGENTS_START = "<!-- DARKFAC:START managed project-adoption-v1 -->"
AGENTS_END = "<!-- DARKFAC:END managed project-adoption-v1 -->"
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
_SEEDABLE_FILES = frozenset({"MISSION.md", "FACTORY_RULES.md", "harness.config.json"})


class AdoptionError(RuntimeError):
    """Base error for project adoption failures."""


class AdoptionBlockedError(AdoptionError):
    """Raised when a plan contains a conflict or unsafe precondition."""

    def __init__(self, blockers: Sequence[str]) -> None:
        self.blockers = tuple(blockers)
        super().__init__("adoption blocked: " + "; ".join(self.blockers))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _run_git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip() or "unknown git error"
        raise AdoptionError(f"git {' '.join(arguments)} failed: {message}")
    return process


def inspect_git(project_root: Path) -> GitSnapshot:
    """Capture Git facts without mutating the repository."""

    probe = _run_git(project_root, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode != 0:
        return GitSnapshot(repository=False)
    root = Path(probe.stdout.strip()).resolve()
    head = _run_git(root, "rev-parse", "HEAD").stdout.strip().lower()
    branch_process = _run_git(root, "branch", "--show-current")
    branch = branch_process.stdout.strip() or None
    status = _run_git(root, "status", "--porcelain=v1", "-z").stdout
    dirty_paths: list[str] = []
    for item in status.split("\0"):
        if not item:
            continue
        dirty_paths.append(item[3:] if len(item) > 3 else item)
    return GitSnapshot(
        repository=True,
        root=str(root),
        head=head,
        branch=branch,
        dirty_paths=tuple(sorted(dirty_paths)),
    )


def _package_scripts(project_root: Path) -> dict[str, str]:
    package_json = project_root / "package.json"
    if not package_json.is_file():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts", {})
    return scripts if isinstance(scripts, dict) else {}


def discover_stack(project_root: Path) -> StackProfile:
    """Infer only commands backed by canonical files already in the project."""

    ecosystems: list[str] = []
    configs: list[str] = []
    commands: list[str] = []

    python_config = next(
        (name for name in ("pyproject.toml", "requirements.txt", "setup.py") if (project_root / name).is_file()),
        None,
    )
    if python_config:
        ecosystems.append("python")
        configs.append(python_config)
        test_targets = [name for name in ("tests", "eval") if (project_root / name).is_dir()]
        if test_targets:
            commands.append("python -m pytest " + " ".join(test_targets))
        pyproject = project_root / "pyproject.toml"
        if pyproject.is_file():
            try:
                configured_tools = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                configured_tools = {}
            if isinstance(configured_tools, dict) and "ruff" in configured_tools:
                commands.insert(0, "python -m ruff check .")
            if isinstance(configured_tools, dict) and "pyright" in configured_tools:
                commands.insert(0, "python -m pyright")

    if (project_root / "package.json").is_file():
        ecosystems.append("node")
        configs.append("package.json")
        scripts = _package_scripts(project_root)
        for script in ("test", "lint", "typecheck"):
            if script in scripts:
                commands.append(f"npm run {script}")

    if (project_root / "Cargo.toml").is_file():
        ecosystems.append("rust")
        configs.append("Cargo.toml")
        commands.append("cargo test")

    if (project_root / "go.mod").is_file():
        ecosystems.append("go")
        configs.append("go.mod")
        commands.append("go test ./...")

    return StackProfile(
        ecosystems=tuple(ecosystems),
        configuration_files=tuple(configs),
        validation_commands=tuple(dict.fromkeys(commands)),
    )


def inspect_project(project_root: Path) -> ProjectInspection:
    """Inspect a project without opening corpus, indexes, credentials, or user data."""

    root = project_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise AdoptionError(f"project root is not a directory: {root}")
    ignored = {".git", ".pytest_cache", ".ruff_cache", ".venv", "venv", "__pycache__"}
    product_entries = [entry for entry in root.iterdir() if entry.name not in ignored]
    governance = tuple(
        name for name in ("AGENTS.md", "MISSION.md", "FACTORY_RULES.md") if (root / name).is_file()
    )
    return ProjectInspection(
        project_root=str(root),
        project_name=root.name,
        kind=ProjectKind.BROWNFIELD if product_entries else ProjectKind.GREENFIELD,
        git=inspect_git(root),
        stack=discover_stack(root),
        governance_files=governance,
        existing_lock=(root / LOCK_PATH).is_file(),
    )


def _source_identity(source_root: Path) -> tuple[str, str]:
    git = inspect_git(source_root)
    if not git.repository or not git.head:
        raise AdoptionBlockedError(("Dark Factory source must be a Git repository with a commit",))
    if not git.clean:
        raise AdoptionBlockedError(
            ("Dark Factory source is dirty; commit the exact runtime before producing provenance",)
        )
    remote = _run_git(source_root, "remote", "get-url", "origin", check=False)
    repository = remote.stdout.strip() if remote.returncode == 0 else "local-git"
    if not repository or re.match(r"^[A-Za-z]:[\\/]", repository):
        repository = "local-git"
    return git.head, repository


def _runtime_wrapper() -> bytes:
    return (
        "#!/usr/bin/env python3\n"
        '"""Stable entrypoint for the namespaced Dark Factory runtime."""\n'
        "from __future__ import annotations\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "runtime = Path(__file__).resolve().parent / 'runtime'\n"
        "os.environ.setdefault('DARKFAC_PROJECT_ROOT', str(Path(__file__).resolve().parent.parent))\n"
        "sys.path.insert(0, str(runtime))\n"
        "if len(sys.argv) < 2:\n"
        "    raise SystemExit('usage: darkfac.py <module|script|harness> ...')\n"
        "mode = sys.argv.pop(1)\n"
        "if mode == 'harness':\n"
        "    sys.argv[0] = 'core.harness.runner'\n"
        "    runpy.run_module('core.harness.runner', run_name='__main__', alter_sys=True)\n"
        "elif mode == 'module' and len(sys.argv) >= 2:\n"
        "    module = sys.argv.pop(1)\n"
        "    sys.argv[0] = module\n"
        "    runpy.run_module(module, run_name='__main__', alter_sys=True)\n"
        "elif mode == 'script' and len(sys.argv) >= 2:\n"
        "    relative = Path(sys.argv.pop(1))\n"
        "    if relative.is_absolute() or '..' in relative.parts:\n"
        "        raise SystemExit('runtime script path must be relative')\n"
        "    script = (runtime / relative).resolve()\n"
        "    if runtime.resolve() not in script.parents:\n"
        "        raise SystemExit('runtime script escapes bundle')\n"
        "    sys.argv[0] = str(script)\n"
        "    runpy.run_path(str(script), run_name='__main__')\n"
        "else:\n"
        "    raise SystemExit(f'unsupported Dark Factory command: {mode}')\n"
    ).encode("utf-8")


def _adapt_skill(content: bytes) -> bytes:
    """Point copied skill commands at the namespaced runtime."""

    text = content.decode("utf-8")
    text = re.sub(
        r"(?m)(?<![\w.])python\s+-m\s+(core\.[A-Za-z0-9_.]+)",
        r"python .factory/darkfac.py module \1",
        text,
    )
    text = re.sub(
        r"(?m)(?<![\w.])python\s+(core/[A-Za-z0-9_./-]+\.py)",
        r"python .factory/darkfac.py script \1",
        text,
    )
    return text.encode("utf-8")


def build_bundle(source_root: Path) -> dict[str, bytes]:
    """Build an immutable, namespaced snapshot from a committed DarkFac checkout."""

    source = source_root.resolve(strict=True)
    payload: dict[str, bytes] = {".factory/darkfac.py": _runtime_wrapper()}

    for path in sorted((source / "core").rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source).as_posix()
        payload[(RUNTIME_ROOT / relative).as_posix()] = path.read_bytes()

    skills_root = source / ".agents" / "skills"
    for path in sorted(skills_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(skills_root).as_posix()
        data = _adapt_skill(path.read_bytes()) if path.suffix.casefold() == ".md" else path.read_bytes()
        payload[f".agents/skills/{relative}"] = data
        payload[f".claude/skills/{relative}"] = data

    for name in ("DARK_FACTORY_PLAYBOOK.md", "HARNESS_INTEROP.md", "MODEL_SELECTION_GUIDE.md"):
        path = source / "docs" / name
        if path.is_file():
            payload[f".factory/docs/{name}"] = path.read_bytes()
    return payload


def _filter_ignored_bundle(
    target: Path,
    payload: Mapping[str, bytes],
) -> tuple[dict[str, bytes], tuple[str, ...], tuple[str, ...]]:
    """Respect consumer ignore policy while keeping required runtime files portable."""

    if not payload:
        return {}, (), ()
    process = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=target,
        input="\0".join(payload) + "\0",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode not in {0, 1}:
        message = process.stderr.strip() or "git check-ignore failed"
        raise AdoptionError(message)
    ignored = {item for item in process.stdout.split("\0") if item}
    filtered = dict(payload)
    blockers: list[str] = []
    warnings: list[str] = []
    for relative in sorted(ignored):
        if relative.startswith(".claude/skills/"):
            filtered.pop(relative, None)
            warnings.append("optional .claude/skills mirror omitted because the consumer ignores it")
        else:
            blockers.append(f"required managed path is ignored by the consumer: {relative}")
    return filtered, tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(warnings))


def _agents_block(autonomy_level: int) -> str:
    return (
        f"{AGENTS_START}\n"
        "## Dark Factory project runtime\n\n"
        "This repository owns its product rules. Dark Factory is an isolated, pinned runtime under "
        "`.factory/runtime/`; never import a sibling DarkFac checkout. Read `MISSION.md` and "
        "`FACTORY_RULES.md` before writing. Use `.agents/skills/` for workflows and run "
        "`python .factory/darkfac.py harness --quick` as the deterministic gate.\n\n"
        f"Autonomy level: **{autonomy_level}**. Merge, deployment, scheduling, credentials and paid "
        "calls require the permissions declared by this project's governance.\n"
        f"{AGENTS_END}"
    )


def _merge_agents(existing: bytes | None, autonomy_level: int) -> tuple[bytes, str | None]:
    block = _agents_block(autonomy_level)
    if existing is None:
        return (block + "\n").encode("utf-8"), None
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError:
        return b"", "AGENTS.md is not valid UTF-8"
    starts = text.count(AGENTS_START)
    ends = text.count(AGENTS_END)
    if starts != ends or starts > 1:
        return b"", "AGENTS.md contains malformed or duplicate Dark Factory managed markers"
    if starts == 1:
        before, remainder = text.split(AGENTS_START, 1)
        _old, after = remainder.split(AGENTS_END, 1)
        merged = before.rstrip() + "\n\n" + block + after
    else:
        merged = text.rstrip() + "\n\n" + block + "\n"
    return merged.encode("utf-8"), None


def _default_mission(name: str) -> bytes:
    return (
        f"# {name} Mission\n\n"
        "## Objective\n\nTODO: define the user outcome before autonomous implementation.\n\n"
        "## Non-goals\n\nTODO: define permanent scope boundaries.\n\n"
        "## Success criterion\n\nTODO: define a headless, observable acceptance path.\n"
    ).encode("utf-8")


def _default_rules() -> bytes:
    return (
        "# Factory Rules\n\n"
        "1. Read `MISSION.md` and `AGENTS.md` before writing.\n"
        "2. Use a task-specific branch; never overwrite unrelated work.\n"
        "3. Keep domain logic headless and validate the affected behavior.\n"
        "4. Do not store secrets, private data, caches, generated artifacts or model weights.\n"
        "5. A task passes only with the configured harness and a positive test count.\n"
        "6. Merge, deployment, scheduling and paid calls require explicit project authorization.\n"
    ).encode("utf-8")


def _default_harness(commands: Sequence[str]) -> bytes:
    steps = [
        {"name": f"project_validation_{index}", "cmd": command, "quick": True, "kind": "test", "timeout_sec": 600}
        for index, command in enumerate(commands, start=1)
    ]
    return (json.dumps({"steps": steps}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _load_lock(path: Path) -> AdoptionLock | None:
    if not path.is_file():
        return None
    try:
        return AdoptionLock.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise AdoptionError(f"invalid adoption lock {path}: {exc}") from exc


def plan_adoption(
    project_root: Path,
    *,
    source_root: Path,
    autonomy_level: int = 2,
    seed_files: Mapping[str, bytes] | None = None,
) -> AdoptionPlan:
    """Produce a complete plan without modifying the target."""

    seeds = dict(seed_files or {})
    invalid_seeds = sorted(set(seeds) - _SEEDABLE_FILES)
    if invalid_seeds:
        raise AdoptionError(f"unsupported project seed files: {', '.join(invalid_seeds)}")
    inspection = inspect_project(project_root)
    target = Path(inspection.project_root)
    source_commit, source_repository = _source_identity(source_root)
    payload, ignore_blockers, ignore_warnings = _filter_ignored_bundle(
        target,
        build_bundle(source_root),
    )
    blockers: list[str] = []
    warnings: list[str] = []
    blockers.extend(ignore_blockers)
    warnings.extend(ignore_warnings)
    files: list[PlannedFile] = []

    if not inspection.git.repository:
        blockers.append("target is not a Git repository; initialize and commit a baseline first")
    elif not inspection.git.clean:
        blockers.append("target worktree is dirty; prepare an isolated adoption worktree from a commit")

    try:
        previous = _load_lock(target / LOCK_PATH)
    except AdoptionError as exc:
        previous = None
        blockers.append(str(exc))
    previous_hashes = previous.managed_files if previous else {}

    for relative, desired in sorted(payload.items()):
        current = _read_bytes(target / relative)
        desired_hash = _sha256(desired)
        current_hash = _sha256(current) if current is not None else None
        previous_hash = previous_hashes.get(relative)
        if current is None:
            action, reason = FileAction.CREATE, "managed file is absent"
        elif current_hash == desired_hash:
            action = FileAction.UNCHANGED if previous_hash else FileAction.ADOPT
            reason = "content already matches the pinned bundle"
        elif previous_hash and current_hash == previous_hash:
            action, reason = FileAction.UPDATE, "previous managed version is unchanged locally"
        else:
            action, reason = FileAction.CONFLICT, "existing content is not an unchanged managed version"
            blockers.append(f"managed path conflict: {relative}")
        files.append(
            PlannedFile(
                path=relative,
                action=action,
                desired_sha256=desired_hash,
                current_sha256=current_hash,
                reason=reason,
            )
        )

    for relative, previous_hash in sorted(previous_hashes.items()):
        if relative in payload:
            continue
        current = _read_bytes(target / relative)
        current_hash = _sha256(current) if current is not None else None
        if current is None:
            action, reason = FileAction.UNCHANGED, "retired managed file is already absent"
        elif current_hash == previous_hash:
            action, reason = FileAction.REMOVE, "file was retired and remains unchanged locally"
        else:
            action, reason = FileAction.CONFLICT, "retired managed file contains local changes"
            blockers.append(f"retired managed path conflict: {relative}")
        files.append(
            PlannedFile(
                path=relative,
                action=action,
                current_sha256=current_hash,
                reason=reason,
            )
        )

    agents_current = _read_bytes(target / "AGENTS.md")
    agents_desired, agents_error = _merge_agents(agents_current, autonomy_level)
    if agents_error:
        blockers.append(agents_error)
        agents_action = FileAction.CONFLICT
    elif agents_current == agents_desired:
        agents_action = FileAction.UNCHANGED
    else:
        agents_action = FileAction.CREATE if agents_current is None else FileAction.UPDATE
    files.append(
        PlannedFile(
            path="AGENTS.md",
            action=agents_action,
            desired_sha256=_sha256(agents_desired) if not agents_error else None,
            current_sha256=_sha256(agents_current) if agents_current is not None else None,
            reason=agents_error or "bounded managed block; product instructions remain outside it",
        )
    )

    for name, fallback in (
        ("MISSION.md", _default_mission(inspection.project_name)),
        ("FACTORY_RULES.md", _default_rules()),
    ):
        content = seeds.get(name, fallback)
        current = _read_bytes(target / name)
        action = FileAction.CREATE if current is None else FileAction.PRESERVE
        files.append(
            PlannedFile(
                path=name,
                action=action,
                desired_sha256=_sha256(content) if current is None else None,
                current_sha256=_sha256(current) if current is not None else None,
                reason="create missing governance" if current is None else "project-owned governance is preserved",
            )
        )
        if current is None and name not in seeds:
            warnings.append(f"{name} contains TODOs and requires project-owner review before autonomous work")

    harness = target / "harness.config.json"
    current_harness = _read_bytes(harness)
    if current_harness is None:
        generated = seeds.get("harness.config.json")
        if generated is None and inspection.stack.validation_commands:
            generated = _default_harness(inspection.stack.validation_commands)
        if generated is not None:
            try:
                parsed_harness = json.loads(generated.decode("utf-8"))
                seed_steps = parsed_harness.get("steps", []) if isinstance(parsed_harness, dict) else []
                if not seed_steps or not any(
                    isinstance(step, dict) and step.get("cmd") for step in seed_steps
                ):
                    raise ValueError("no executable checks")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                blockers.append(f"seeded harness.config.json is invalid: {exc}")
            files.append(
                PlannedFile(
                    path="harness.config.json",
                    action=FileAction.CREATE,
                    desired_sha256=_sha256(generated),
                    reason=(
                        "seeded from an explicitly selected project contract"
                        if "harness.config.json" in seeds
                        else "generated from detected canonical test commands"
                    ),
                )
            )
        else:
            blockers.append("no deterministic validation command was discovered")
    else:
        try:
            raw = json.loads(current_harness.decode("utf-8"))
            steps = raw.get("steps", []) if isinstance(raw, dict) else []
            if not steps or not any(isinstance(step, dict) and step.get("cmd") for step in steps):
                blockers.append("existing harness.config.json contains no executable checks")
        except (UnicodeDecodeError, json.JSONDecodeError):
            blockers.append("existing harness.config.json is not valid UTF-8 JSON")
        files.append(
            PlannedFile(
                path="harness.config.json",
                action=FileAction.PRESERVE,
                current_sha256=_sha256(current_harness),
                reason="project-owned validation contract is preserved",
            )
        )

    return AdoptionPlan(
        inspection=inspection,
        source_commit=source_commit,
        source_repository=source_repository,
        autonomy_level=autonomy_level,
        files=tuple(files),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _desired_content(
    item: PlannedFile,
    *,
    payload: Mapping[str, bytes],
    target: Path,
    autonomy_level: int,
    commands: Sequence[str],
    seed_files: Mapping[str, bytes],
) -> bytes | None:
    if item.path in payload:
        return payload[item.path]
    if item.path == "AGENTS.md":
        merged, error = _merge_agents(_read_bytes(target / item.path), autonomy_level)
        if error:
            raise AdoptionBlockedError((error,))
        return merged
    if item.path == "MISSION.md" and item.action == FileAction.CREATE:
        return seed_files.get(item.path, _default_mission(target.name))
    if item.path == "FACTORY_RULES.md" and item.action == FileAction.CREATE:
        return seed_files.get(item.path, _default_rules())
    if item.path == "harness.config.json" and item.action == FileAction.CREATE:
        return seed_files.get(item.path, _default_harness(commands))
    return None


def apply_adoption(
    project_root: Path,
    *,
    source_root: Path,
    autonomy_level: int = 2,
    seed_files: Mapping[str, bytes] | None = None,
) -> AdoptionResult:
    """Apply a ready plan atomically enough to roll back every touched file."""

    seeds = dict(seed_files or {})
    plan = plan_adoption(
        project_root,
        source_root=source_root,
        autonomy_level=autonomy_level,
        seed_files=seeds,
    )
    if not plan.ready:
        raise AdoptionBlockedError(plan.blockers)
    target = Path(plan.inspection.project_root)
    payload, ignore_blockers, _ignore_warnings = _filter_ignored_bundle(
        target,
        build_bundle(source_root),
    )
    if ignore_blockers:
        raise AdoptionBlockedError(ignore_blockers)
    commands = plan.inspection.stack.validation_commands
    originals: dict[Path, bytes | None] = {}
    created: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    adopted: list[str] = []
    unchanged: list[str] = []
    managed_hashes: dict[str, str] = {}

    try:
        for item in plan.files:
            if item.action == FileAction.PRESERVE:
                continue
            if item.action == FileAction.REMOVE:
                destination = target / item.path
                originals[destination] = _read_bytes(destination)
                destination.unlink(missing_ok=True)
                removed.append(item.path)
                continue
            if item.action in (FileAction.UNCHANGED, FileAction.ADOPT):
                if item.action == FileAction.ADOPT:
                    adopted.append(item.path)
                else:
                    unchanged.append(item.path)
                if item.path in payload:
                    managed_hashes[item.path] = item.desired_sha256 or ""
                continue
            data = _desired_content(
                item,
                payload=payload,
                target=target,
                autonomy_level=autonomy_level,
                commands=commands,
                seed_files=seeds,
            )
            if data is None:
                continue
            destination = target / item.path
            originals[destination] = _read_bytes(destination)
            _atomic_write(destination, data)
            (created if item.action == FileAction.CREATE else updated).append(item.path)
            if item.path in payload:
                managed_hashes[item.path] = _sha256(data)

        for relative, data in payload.items():
            managed_hashes.setdefault(relative, _sha256(data))
        block_hash = _sha256(_agents_block(autonomy_level).encode("utf-8"))
        lock = AdoptionLock(
            source_repository=plan.source_repository,
            source_commit=plan.source_commit,
            autonomy_level=autonomy_level,
            managed_files=dict(sorted(managed_hashes.items())),
            seeded_project_files={name: _sha256(data) for name, data in sorted(seeds.items())},
            agents_block_sha256=block_hash,
            validation_commands=commands,
        )
        lock_path = target / LOCK_PATH
        originals[lock_path] = _read_bytes(lock_path)
        _atomic_write(lock_path, (lock.model_dump_json(indent=2) + "\n").encode("utf-8"))
    except Exception:
        for path, original in reversed(tuple(originals.items())):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, original)
        raise

    return AdoptionResult(
        project_root=str(target),
        source_commit=plan.source_commit,
        created=tuple(created),
        updated=tuple(updated),
        removed=tuple(removed),
        adopted=tuple(adopted),
        unchanged=tuple(unchanged),
        lock_path=LOCK_PATH.as_posix(),
    )


def initialize_project(project_root: Path, *, project_name: str | None = None) -> Path:
    """Create a committed Git baseline suitable for transactional adoption."""

    root = project_root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise AdoptionBlockedError((f"greenfield destination is not empty: {root}",))
    root.mkdir(parents=True, exist_ok=True)
    _run_git(root, "init")
    name = (project_name or root.name).strip()
    if not name:
        raise AdoptionBlockedError(("project name is required",))
    _atomic_write(root / "README.md", f"# {name}\n".encode("utf-8"))
    _run_git(root, "add", "README.md")
    _run_git(
        root,
        "-c",
        "user.name=DarkFac",
        "-c",
        "user.email=darkfac@local",
        "commit",
        "-m",
        "chore: initialize project baseline",
    )
    return root


def _extract_agents_block(data: bytes) -> bytes | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.count(AGENTS_START) != 1 or text.count(AGENTS_END) != 1:
        return None
    _before, remainder = text.split(AGENTS_START, 1)
    middle, _after = remainder.split(AGENTS_END, 1)
    return f"{AGENTS_START}{middle}{AGENTS_END}".encode("utf-8")


def verify_adoption(project_root: Path) -> VerificationReport:
    """Verify provenance, managed-file drift, governance hook, and test contract."""

    root = project_root.expanduser().resolve(strict=True)
    problems: list[str] = []
    try:
        lock = _load_lock(root / LOCK_PATH)
    except AdoptionError as exc:
        return VerificationReport(project_root=str(root), lock_valid=False, problems=(str(exc),))
    if lock is None:
        return VerificationReport(project_root=str(root), lock_valid=False, problems=("adoption lock is missing",))

    checked = 0
    for relative, expected in sorted(lock.managed_files.items()):
        current = _read_bytes(root / relative)
        if current is None:
            problems.append(f"managed file missing: {relative}")
            continue
        checked += 1
        if _sha256(current) != expected:
            problems.append(f"managed file drift: {relative}")

    agents = _read_bytes(root / "AGENTS.md")
    block = _extract_agents_block(agents or b"")
    if block is None:
        problems.append("AGENTS.md managed block is missing or malformed")
    elif _sha256(block) != lock.agents_block_sha256:
        problems.append("AGENTS.md managed block drift")
    if not (root / "harness.config.json").is_file():
        problems.append("harness.config.json is missing")
    git = inspect_git(root)
    if not git.repository:
        problems.append("target is not a Git repository")
    return VerificationReport(
        project_root=str(root),
        lock_valid=True,
        managed_files_checked=checked,
        problems=tuple(problems),
    )


def prepare_adoption_worktree(
    project_root: Path,
    *,
    branch: str = "codex/darkfac-adoption",
    destination: Path | None = None,
    base_ref: str = "HEAD",
) -> Path:
    """Create a clean adoption workspace from committed state, even if the main checkout is dirty."""

    root = project_root.expanduser().resolve(strict=True)
    git = inspect_git(root)
    if not git.repository:
        raise AdoptionBlockedError(("target is not a Git repository",))
    base_sha = _run_git(root, "rev-parse", f"{base_ref}^{{commit}}").stdout.strip()
    branch_probe = _run_git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if branch_probe.returncode == 0:
        raise AdoptionBlockedError((f"branch already exists: {branch}",))
    target = destination or (root.parent / f"{root.name}_worktrees" / branch.replace("/", "-"))
    target = target.expanduser().resolve()
    if target.exists():
        raise AdoptionBlockedError((f"worktree destination already exists: {target}",))
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(root, "worktree", "add", "--lock", "--reason", "DarkFac adoption transaction", "-b", branch, str(target), base_sha)
    return target


def prepare_task(
    project_root: Path,
    *,
    ticket_id: str,
    title: str,
    owner: str,
    allowed_paths: Sequence[str],
    validate_commands: Sequence[str],
    branch: str | None = None,
    destination: Path | None = None,
    base_ref: str = "HEAD",
) -> TaskPreparation:
    """Create an isolated worktree and durable task handoff from an adopted commit."""

    root = project_root.expanduser().resolve(strict=True)
    report = verify_adoption(root)
    if not report.ready:
        raise AdoptionBlockedError(report.problems)
    normalized_ticket = ticket_id.strip().upper()
    if not _TICKET_RE.fullmatch(normalized_ticket):
        raise AdoptionBlockedError(("ticket id must be a stable uppercase hyphenated identifier",))
    if not owner.strip() or not title.strip():
        raise AdoptionBlockedError(("task owner and title are required",))
    clean_paths = tuple(dict.fromkeys(path.strip().replace("\\", "/") for path in allowed_paths if path.strip()))
    clean_commands = tuple(dict.fromkeys(command.strip() for command in validate_commands if command.strip()))
    if not clean_paths or not clean_commands:
        raise AdoptionBlockedError(("task requires owned paths and validation commands",))
    task_branch = branch or f"codex/{normalized_ticket.casefold()}"
    base_sha = _run_git(root, "rev-parse", f"{base_ref}^{{commit}}").stdout.strip()
    worktree = prepare_adoption_worktree(
        root,
        branch=task_branch,
        destination=destination,
        base_ref=base_sha,
    )
    manifest_relative = Path(".factory/tasks") / f"{normalized_ticket.casefold()}.json"
    manifest = {
        "schema_version": 1,
        "ticket_id": normalized_ticket,
        "title": title.strip(),
        "owner": owner.strip(),
        "branch": task_branch,
        "worktree": str(worktree),
        "base_sha": base_sha,
        "allowed_paths": clean_paths,
        "validate_commands": clean_commands,
        "status": "planned",
    }
    try:
        _atomic_write(
            worktree / manifest_relative,
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    except Exception:
        _run_git(root, "worktree", "unlock", str(worktree), check=False)
        _run_git(root, "worktree", "remove", str(worktree), check=False)
        raise
    return TaskPreparation(
        ticket_id=normalized_ticket,
        title=title.strip(),
        project_root=str(root),
        branch=task_branch,
        worktree=str(worktree),
        base_sha=base_sha,
        owner=owner.strip(),
        allowed_paths=clean_paths,
        validate_commands=clean_commands,
        manifest_path=manifest_relative.as_posix(),
    )
