#!/usr/bin/env python3
"""Stateless, deterministic test-impact selector for the DarkFac suite.

Given a base ref, this module computes the set of changed files versus the
merge-base with ``HEAD`` (plus uncommitted working-tree changes and
untracked files), builds a static import graph over the repository via
``ast``, and selects the subset of test files whose transitive import
closure -- or a literal textual reference -- touches a changed file.

The selector never relies on prior test-run state (no coverage database):
every worktree, on every host, produces the same answer from the same git
diff. When the analysis cannot be confident (missing/broken graph,
governance or config files, a module imported by most of the suite, an
unmatched runtime asset) it conservatively escalates to the full suite.

Usage::

    python -m core.harness.affected [--base REF] [--list | --json] [--run] [--explain PATH]
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

LOGGER = logging.getLogger("core.harness.affected")

# --- UTF-8 safe output on Windows consoles -----------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- constants ----------------------------------------------------------------

#: Top-level directories scanned for the import graph.
SCAN_ROOT_DIRS = ("core", "hub", "scripts", "tests")
#: Standalone top-level files scanned for the import graph.
SCAN_ROOT_FILES = ("run_hub.py",)

#: Directory names never descended into while scanning for .py files.
SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "_reference_skills",
    "spikes",
    "audio_bench",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
}
#: Path substrings (posix-normalized) that mark another worktree or scratch
#: tree; anything containing one of these is skipped defensively even though
#: none of SCAN_ROOT_DIRS is expected to nest one.
SKIP_PATH_MARKERS = (
    "/.claude/worktrees/",
    "/.worktrees/",
    "/.codex/worktrees/",
)

#: Files that DarkFac deliberately keeps untracked/local (FACTORY_RULES.md
#: rule 4: never touch or version the Canaletto experiment). Excluded from
#: both graph scanning and any selected/full test run.
EXCLUDED_TEST_FILES = {"tests/test_canaletto.py"}

#: Changed files that always force a full-suite run because they affect
#: test collection/config globally rather than a traceable subset.
FULL_ESCALATION_EXACT_FILES = {
    "pytest.ini",
    "tests/conftest.py",
    "harness.config.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
}
FULL_ESCALATION_PREFIXES = ("requirements",)

#: Fraction of test files importing a module above which that module is
#: considered "hot" -- changing it is treated like a config/governance file.
HOT_MODULE_THRESHOLD = 0.5

#: Non-Python basenames considered too generic to match on alone; for these,
#: the full repo-relative path must appear in a test's source.
GENERIC_BASENAMES = {
    "index.html",
    "readme.md",
    "config.json",
    "__init__.py",
    "settings.json",
    "package.json",
}
GENERIC_BASENAME_MIN_LEN = 6

WORKFLOW_DIR_PREFIX = ".github/workflows/"
CI_POLICY_TEST = "tests/test_ci_policy.py"

NON_PYTHON_MATCHABLE_ROOTS = ("core/", "hub/")


# --- data model -----------------------------------------------------------


@dataclass
class FileInfo:
    """One scanned repository Python file."""

    path: str  # repo-relative, posix separators
    dotted: str  # dotted module name (package name for __init__.py)
    is_init: bool
    source: str
    raw_imports: set[str] = field(default_factory=set)  # every dotted candidate referenced
    edges: set[str] = field(default_factory=set)  # resolved internal deps (repo-relative paths)


@dataclass
class ImportGraph:
    files: dict[str, FileInfo]  # keyed by repo-relative posix path
    dotted_to_path: dict[str, str]
    test_paths: list[str]


@dataclass
class Selection:
    mode: str  # "none" | "subset" | "full"
    tests: list[str]
    reasons: dict[str, list[str]]
    escalations: list[str]
    changed_files: list[str]


class GraphBuildError(RuntimeError):
    """Raised when the static import graph cannot be built reliably."""


# --- git plumbing ----------------------------------------------------------


def _run_git(repo_root: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, args, output=result.stdout, stderr=result.stderr
        )
    return result.stdout


def _git_ref_exists(repo_root: Path, ref: str) -> bool:
    try:
        _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        return True
    except subprocess.CalledProcessError:
        return False


def find_repo_root(start: Path | None = None) -> Path:
    start = start or Path.cwd()
    out = _run_git(start, ["rev-parse", "--show-toplevel"])
    return Path(out.strip()).resolve()


def resolve_base_ref(repo_root: Path, requested_base: str) -> str:
    """Resolve ``requested_base``, falling back to main then HEAD~1."""

    for candidate in (requested_base, "main", "HEAD~1"):
        if candidate and _git_ref_exists(repo_root, candidate):
            return candidate
    raise GraphBuildError(f"no usable base ref found (tried {requested_base!r}, main, HEAD~1)")


def compute_merge_base(repo_root: Path, base_ref: str) -> str:
    out = _run_git(repo_root, ["merge-base", base_ref, "HEAD"])
    return out.strip()


def get_changed_files(repo_root: Path, merge_base: str) -> list[str]:
    """Changed files vs merge-base (working tree included) + untracked files."""

    diff_out = _run_git(repo_root, ["diff", "--name-only", merge_base])
    changed = {line.strip() for line in diff_out.splitlines() if line.strip()}

    untracked_out = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    changed |= {line.strip() for line in untracked_out.splitlines() if line.strip()}

    return sorted(p.replace("\\", "/") for p in changed)


# --- filesystem scanning ----------------------------------------------------


def _is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


def _iter_scan_files(repo_root: Path) -> Iterable[Path]:
    for root_name in SCAN_ROOT_DIRS:
        root_dir = repo_root / root_name
        if not root_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in _walk(root_dir):
            dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
            for filename in filenames:
                if filename.endswith(".py"):
                    candidate = dirpath / filename
                    rel = candidate.relative_to(repo_root).as_posix()
                    if any(marker.strip("/") in rel for marker in SKIP_PATH_MARKERS):
                        continue
                    if rel in EXCLUDED_TEST_FILES:
                        continue
                    yield candidate
    for file_name in SCAN_ROOT_FILES:
        candidate = repo_root / file_name
        if candidate.is_file():
            yield candidate


def _walk(root_dir: Path):
    import os

    for dirpath_str, dirnames, filenames in os.walk(root_dir):
        yield Path(dirpath_str), dirnames, filenames


def path_to_dotted(rel_posix: str) -> tuple[str, bool]:
    """Return (dotted_module_name, is_init) for a repo-relative .py path."""

    without_ext = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    parts = without_ext.split("/")
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    if not parts:
        # Top-level __init__.py (unlikely) -- fall back to empty package name.
        return "", True
    return ".".join(parts), is_init


# --- import parsing ----------------------------------------------------------


def _package_of(dotted: str, is_init: bool) -> str:
    if is_init:
        return dotted
    if "." not in dotted:
        return ""
    return dotted.rsplit(".", 1)[0]


def _resolve_relative_base(package: str, level: int) -> str | None:
    if level <= 0:
        return package
    components = package.split(".") if package else []
    strip_count = level - 1
    if strip_count > len(components):
        return None
    remaining = components[: len(components) - strip_count]
    return ".".join(remaining)


def _extract_raw_imports(tree: ast.AST, own_dotted: str, own_is_init: bool) -> set[str]:
    package = _package_of(own_dotted, own_is_init)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
                # Also record each dotted prefix, e.g. "a.b.c" -> "a", "a.b".
                parts = alias.name.split(".")
                for i in range(1, len(parts)):
                    found.add(".".join(parts[:i]))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = _resolve_relative_base(package, node.level)
            else:
                base = node.module or ""
            if base is None:
                continue
            if node.module and node.level:
                base = f"{base}.{node.module}" if base else node.module
            if base:
                found.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                combo = f"{base}.{alias.name}" if base else alias.name
                found.add(combo)
        elif isinstance(node, ast.Call):
            func = node.func
            is_import_module_call = (
                (isinstance(func, ast.Attribute) and func.attr == "import_module")
                or (isinstance(func, ast.Name) and func.id == "import_module")
            )
            if is_import_module_call and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)

    return found


def build_import_graph(repo_root: Path) -> ImportGraph:
    files: dict[str, FileInfo] = {}
    dotted_to_path: dict[str, str] = {}

    for abs_path in _iter_scan_files(repo_root):
        rel = abs_path.relative_to(repo_root).as_posix()
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GraphBuildError(f"cannot read {rel}: {exc}") from exc
        dotted, is_init = path_to_dotted(rel)
        info = FileInfo(path=rel, dotted=dotted, is_init=is_init, source=source)
        files[rel] = info
        if dotted:
            # First writer wins; duplicate dotted names should not happen in
            # a well-formed repo layout.
            dotted_to_path.setdefault(dotted, rel)

    for info in files.values():
        try:
            tree = ast.parse(info.source, filename=info.path)
        except SyntaxError as exc:
            LOGGER.warning("skipping unparsable file %s: %s", info.path, exc)
            continue
        info.raw_imports = _extract_raw_imports(tree, info.dotted, info.is_init)

    # Resolve edges, including ancestor-package __init__ execution edges.
    for info in files.values():
        for candidate in info.raw_imports:
            target_path = dotted_to_path.get(candidate)
            if target_path is None or target_path == info.path:
                continue
            info.edges.add(target_path)
            for ancestor_path in _ancestor_init_paths(candidate, dotted_to_path):
                if ancestor_path != info.path:
                    info.edges.add(ancestor_path)

    test_paths = sorted(
        p
        for p in files
        if p.startswith("tests/") and Path(p).name.startswith("test_") and p not in EXCLUDED_TEST_FILES
    )

    return ImportGraph(files=files, dotted_to_path=dotted_to_path, test_paths=test_paths)


def _ancestor_init_paths(dotted: str, dotted_to_path: dict[str, str]) -> list[str]:
    parts = dotted.split(".")
    out = []
    for i in range(1, len(parts)):
        ancestor_dotted = ".".join(parts[:i])
        ancestor_path = dotted_to_path.get(ancestor_dotted)
        if ancestor_path and Path(ancestor_path).name == "__init__.py":
            out.append(ancestor_path)
    return out


def transitive_closure(start: str, graph: ImportGraph) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        info = graph.files.get(current)
        if info is None:
            continue
        for nxt in info.edges:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# --- changed-file classification -------------------------------------------


def _changed_module_identity(changed_path: str) -> tuple[str | None, bool]:
    """Return (dotted_name, is_init) for a changed path if it is a .py file
    under a scanned root; else (None, False)."""

    if not changed_path.endswith(".py"):
        return None, False
    top = changed_path.split("/", 1)[0]
    if changed_path not in SCAN_ROOT_FILES and top not in SCAN_ROOT_DIRS:
        return None, False
    dotted, is_init = path_to_dotted(changed_path)
    return dotted, is_init


def _text_reference_hit(source: str, changed_path: str, dotted: str | None) -> bool:
    if changed_path in source:
        return True
    if changed_path.replace("/", "\\") in source:
        return True
    if dotted and dotted in source:
        return True
    return False


def _is_conftest_or_test_helper(path: str) -> bool:
    if not path.startswith("tests/"):
        return False
    name = Path(path).name
    if name.startswith("test_"):
        return False
    if name == "conftest.py":
        return True
    # fixtures/helpers, e.g. tests/fixtures/*, tests/line/fixture_*.py
    return name.endswith(".py")


def _governance_escalation(changed_path: str) -> str | None:
    if changed_path in FULL_ESCALATION_EXACT_FILES:
        return f"governance_file: {changed_path}"
    basename = Path(changed_path).name
    if basename.startswith(FULL_ESCALATION_PREFIXES) and basename.endswith(".txt"):
        return f"governance_file: {changed_path}"
    return None


def compute_test_closures(graph: ImportGraph) -> dict[str, set[str]]:
    """Transitive import closure per test file, computed once and reused."""

    return {test_path: transitive_closure(test_path, graph) for test_path in graph.test_paths}


def _compute_hot_modules(graph: ImportGraph, test_closures: dict[str, set[str]]) -> set[str]:
    """Repo-relative paths imported (transitively) by > HOT_MODULE_THRESHOLD
    of test files."""

    total_tests = len(graph.test_paths)
    if total_tests == 0:
        return set()
    counts: dict[str, int] = {}
    for closure in test_closures.values():
        for module_path in closure:
            counts[module_path] = counts.get(module_path, 0) + 1
    return {
        path
        for path, count in counts.items()
        if (count / total_tests) > HOT_MODULE_THRESHOLD
    }


def _select_tests_for_python_change(
    graph: ImportGraph,
    test_closures: dict[str, set[str]],
    changed_path: str,
    dotted: str | None,
) -> tuple[set[str], dict[str, list[str]]]:
    selected: set[str] = set()
    reasons: dict[str, list[str]] = {}

    is_test_file = changed_path.startswith("tests/") and Path(changed_path).name.startswith("test_")
    if is_test_file and changed_path in graph.files:
        selected.add(changed_path)
        reasons.setdefault(changed_path, []).append("changed_test_file")

    helper_selected = False
    if _is_conftest_or_test_helper(changed_path):
        helper_selected = True
        changed_dir = str(Path(changed_path).parent.as_posix())
        for test_path in graph.test_paths:
            same_or_nested = test_path.startswith(changed_dir + "/") or str(
                Path(test_path).parent.as_posix()
            ) == changed_dir
            if same_or_nested:
                selected.add(test_path)
                reasons.setdefault(test_path, []).append(
                    f"test_helper_directory: {changed_path}"
                )

    for test_path in graph.test_paths:
        info = graph.files[test_path]
        hit = False
        why: list[str] = []

        if dotted:
            closure = test_closures.get(test_path, {test_path})
            if changed_path in closure:
                hit = True
                why.append(f"imports {changed_path}")
            else:
                # Fallback for references the graph could not resolve to a
                # file node (e.g. the changed file was deleted, or the
                # import target is an attribute rather than a submodule):
                # any file already reachable via the import graph that
                # still textually references the changed dotted name.
                for member_path in sorted(closure):
                    member_info = graph.files.get(member_path)
                    if member_info and dotted in member_info.raw_imports:
                        hit = True
                        why.append(f"imports (unresolved) {dotted} via {member_path}")
                        break

        if _text_reference_hit(info.source, changed_path, dotted):
            hit = True
            why.append(f"text_reference: {changed_path}")

        if hit:
            selected.add(test_path)
            reasons.setdefault(test_path, []).extend(why)

    if not selected and not helper_selected and not is_test_file:
        LOGGER.debug("no tests reference changed python file %s", changed_path)

    return selected, reasons


def _select_tests_for_non_python_change(
    graph: ImportGraph, changed_path: str
) -> tuple[set[str], dict[str, list[str]], str | None]:
    selected: set[str] = set()
    reasons: dict[str, list[str]] = {}
    escalation: str | None = None

    basename = Path(changed_path).name

    if changed_path.startswith(WORKFLOW_DIR_PREFIX):
        if CI_POLICY_TEST in graph.files:
            selected.add(CI_POLICY_TEST)
            reasons.setdefault(CI_POLICY_TEST, []).append(f"ci_workflow_change: {changed_path}")
        for test_path in graph.test_paths:
            if test_path == CI_POLICY_TEST:
                continue
            if changed_path in graph.files[test_path].source:
                selected.add(test_path)
                reasons.setdefault(test_path, []).append(f"text_reference: {changed_path}")
        return selected, reasons, escalation

    if changed_path.startswith("docs/"):
        return selected, reasons, escalation

    basename_is_generic = (
        basename.lower() in GENERIC_BASENAMES or len(basename) < GENERIC_BASENAME_MIN_LEN
    )

    for test_path in graph.test_paths:
        source = graph.files[test_path].source
        matched = changed_path in source or changed_path.replace("/", "\\") in source
        if not matched and not basename_is_generic:
            matched = basename in source
        if matched:
            selected.add(test_path)
            reasons.setdefault(test_path, []).append(f"text_reference: {changed_path}")

    is_runtime_asset = any(changed_path.startswith(prefix) for prefix in NON_PYTHON_MATCHABLE_ROOTS)
    if is_runtime_asset and not selected:
        escalation = f"unmatched_asset: {changed_path}"

    return selected, reasons, escalation


# --- top-level selection ------------------------------------------------------


def select_affected(repo_root: Path, base_ref_requested: str) -> Selection:
    resolved_base = resolve_base_ref(repo_root, base_ref_requested)
    merge_base = compute_merge_base(repo_root, resolved_base)
    changed_files = [f for f in get_changed_files(repo_root, merge_base) if f not in EXCLUDED_TEST_FILES]

    if not changed_files:
        return Selection(mode="none", tests=[], reasons={}, escalations=[], changed_files=[])

    try:
        graph = build_import_graph(repo_root)
    except GraphBuildError as exc:
        return Selection(
            mode="full",
            tests=[],
            reasons={},
            escalations=[f"graph_build_failed: {exc}"],
            changed_files=changed_files,
        )
    except Exception as exc:  # noqa: BLE001 - conservative: any failure escalates
        return Selection(
            mode="full",
            tests=[],
            reasons={},
            escalations=[f"graph_build_failed: {exc!r}"],
            changed_files=changed_files,
        )

    escalations: list[str] = []
    try:
        test_closures = compute_test_closures(graph)
    except Exception as exc:  # noqa: BLE001 - conservative: any failure escalates
        return Selection(
            mode="full",
            tests=sorted(graph.test_paths),
            reasons={},
            escalations=[f"graph_build_failed: {exc!r}"],
            changed_files=changed_files,
        )

    changed_existing_py = {
        f for f in changed_files if f.endswith(".py") and f in graph.files
    }
    hot_modules: set[str] = set()
    if changed_existing_py:
        try:
            hot_modules = _compute_hot_modules(graph, test_closures)
        except Exception as exc:  # noqa: BLE001
            escalations.append(f"graph_build_failed: {exc!r}")

    selected: set[str] = set()
    reasons: dict[str, list[str]] = {}

    for changed_path in changed_files:
        governance_hit = _governance_escalation(changed_path)
        if governance_hit:
            escalations.append(governance_hit)
            continue

        if changed_path.endswith(".py"):
            if changed_path in hot_modules:
                escalations.append(
                    f"hot_module: {changed_path} (imported by >{int(HOT_MODULE_THRESHOLD * 100)}% of tests)"
                )
                continue
            dotted, _is_init = _changed_module_identity(changed_path)
            top = changed_path.split("/", 1)[0]
            if dotted is None and changed_path not in SCAN_ROOT_FILES and top not in SCAN_ROOT_DIRS:
                # Python file outside scanned roots: fall back to text search
                # across all tests, conservatively.
                for test_path in graph.test_paths:
                    if _text_reference_hit(graph.files[test_path].source, changed_path, None):
                        selected.add(test_path)
                        reasons.setdefault(test_path, []).append(f"text_reference: {changed_path}")
                continue
            found, found_reasons = _select_tests_for_python_change(
                graph, test_closures, changed_path, dotted
            )
            selected |= found
            for test_path, why in found_reasons.items():
                reasons.setdefault(test_path, []).extend(why)
        else:
            found, found_reasons, escalation = _select_tests_for_non_python_change(graph, changed_path)
            selected |= found
            for test_path, why in found_reasons.items():
                reasons.setdefault(test_path, []).extend(why)
            if escalation:
                escalations.append(escalation)

    if escalations:
        return Selection(
            mode="full",
            tests=sorted(graph.test_paths),
            reasons={},
            escalations=sorted(set(escalations)),
            changed_files=changed_files,
        )

    for test_path in reasons:
        reasons[test_path] = sorted(set(reasons[test_path]))

    return Selection(
        mode="subset" if selected else "none",
        tests=sorted(selected),
        reasons=reasons,
        escalations=[],
        changed_files=changed_files,
    )


# --- pytest execution ---------------------------------------------------------


def _xdist_available() -> bool:
    return importlib.util.find_spec("xdist") is not None


def run_pytest(
    repo_root: Path,
    selection: Selection,
    extra_args: Sequence[str],
) -> int:
    if selection.mode == "none":
        print("No tests affected by the current changes.")
        return 0

    if selection.mode == "full":
        args = ["tests"] + [f"--ignore={excluded}" for excluded in sorted(EXCLUDED_TEST_FILES)]
        use_xdist = _xdist_available()
    else:
        args = selection.tests
        use_xdist = _xdist_available() and len(args) > 30

    cmd = [sys.executable, "-m", "pytest", *args, "-q"]
    if use_xdist:
        cmd.extend(["-n", "auto", "--dist", "loadfile"])
    cmd.extend(extra_args)

    LOGGER.info("running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(repo_root))
    return result.returncode


# --- CLI -----------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.harness.affected",
        description="Stateless test-impact selector for the DarkFac suite.",
    )
    parser.add_argument("--base", default="origin/main", help="Base ref to diff against (default: origin/main).")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--list", action="store_true", help="Print selected test files, one per line (default).")
    output_group.add_argument("--json", action="store_true", help="Print a machine-readable JSON selection.")
    parser.add_argument("--run", action="store_true", help="Execute pytest against the selected tests.")
    parser.add_argument("--explain", metavar="PATH", help="Explain why a given test file was selected.")
    parser.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to pytest after --run -- (prefix with --).",
    )
    return parser


def _strip_leading_double_dash(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    started = time.monotonic()
    try:
        repo_root = find_repo_root()
    except subprocess.CalledProcessError as exc:
        print(f"error: not a git repository or git unavailable: {exc}", file=sys.stderr)
        return 2

    try:
        selection = select_affected(repo_root, args.base)
    except GraphBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    elapsed = time.monotonic() - started

    if args.explain:
        _print_explain(selection, args.explain)
        return 0

    if args.json:
        payload = {
            "mode": selection.mode,
            "tests": selection.tests,
            "reasons": selection.reasons,
            "escalations": selection.escalations,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if selection.mode == "none":
            print("No tests affected by the current changes.", file=sys.stderr)
        else:
            for test_path in selection.tests:
                print(test_path)

    reason_summary = _summarize_reasons(selection)
    print(
        f"[affected] mode={selection.mode} changed={len(selection.changed_files)} "
        f"tests={len(selection.tests)} time={elapsed:.2f}s {reason_summary}",
        file=sys.stderr,
    )

    if args.run:
        extra = _strip_leading_double_dash(list(args.extra_args))
        return run_pytest(repo_root, selection, extra)

    return 0


def _summarize_reasons(selection: Selection) -> str:
    if selection.mode == "full":
        joined = "; ".join(selection.escalations[:3])
        more = "" if len(selection.escalations) <= 3 else f" (+{len(selection.escalations) - 3} more)"
        return f"escalations={joined}{more}"
    if selection.mode == "none":
        return "no changed file matched any test"
    return f"changed_files={len(selection.changed_files)}"


def _print_explain(selection: Selection, target: str) -> None:
    normalized = target.replace("\\", "/")
    if selection.mode == "full":
        print(f"mode=full: entire suite selected. escalations: {selection.escalations}")
        return
    if normalized not in selection.tests:
        print(f"{normalized} was NOT selected (mode={selection.mode}).")
        return
    reasons = selection.reasons.get(normalized, [])
    print(f"{normalized} was selected. Reasons:")
    for reason in reasons:
        print(f"  - {reason}")


if __name__ == "__main__":
    raise SystemExit(main())
