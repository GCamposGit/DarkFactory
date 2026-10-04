"""USR-87 contract: a missing route never parks a run on a human before the run's own time budget.

`core/line/*.py` is scanned (AST, no imports): `no_route_available` may only sit next to
`waiting_human` in the post-budget path of `RouteWaiter.no_route_result`. Anything else is the
pre-USR-87 bug back again (a `waiting_human` that no mechanism wakes, so the run stalls on a quota
that resets by itself).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

LINE_DIR = Path(__file__).resolve().parents[2] / "core" / "line"

# (file, qualified function) -> why `no_route_available` and `waiting_human` may meet there.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("route_wait.py", "RouteWaiter._park_on_infra"): (
        "the post-budget path: the run already waited `run_caps.wall_clock_hours` for a route, which "
        "is a real infrastructure problem (HumanRequest kind=infra), reached only from "
        "`RouteWaiter.no_route_result` after its wall-clock check"
    ),
    ("owner_intake.py", "_expired_no_route_wait"): (
        "read-only inspection of an existing post-budget waiting_human(no_route_available) "
        "job; it never creates a wait and permits a fresh attempt only after wall_clock_hours"
    ),
}

# Stage modules that pick a route for an agent and therefore MUST go through the waiter.
AGENT_STAGE_MODULES = (
    "stage_build.py",
    "stage_review.py",
    "stage_grill.py",
    "stage_planning.py",
    "stage_integration.py",
)

# Modules that call `pick` on purpose without parking the run on a missing route.
NOT_A_ROUTE_PARK: dict[str, str] = {}


def _literals(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _names(node: ast.AST) -> list[str]:
    found = [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]
    found += [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]
    return found


def _mentions_no_route(node: ast.AST) -> bool:
    return any("no_route_available" in text for text in _literals(node)) or "NO_ROUTE_CAUSE" in _names(node)


def _mentions_waiting_human(node: ast.AST) -> bool:
    return "waiting_human" in _literals(node)


def _scopes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every function (qualified by its classes) plus the module body outside functions."""
    scopes: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((f"{prefix}{child.name}", child))
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return scopes


def _line_files() -> list[Path]:
    return sorted(p for p in LINE_DIR.glob("*.py") if p.name != "__init__.py")


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes of a function without descending into nested functions (they are scopes of their own)."""
    nodes: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))
    return nodes


def _violations_in(filename: str, source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source, filename=filename)
    scopes = _scopes(tree)
    for qualname, scope in scopes:
        wrapper = ast.Module(body=_own_nodes(scope), type_ignores=[])
        if _mentions_no_route(wrapper) and _mentions_waiting_human(wrapper):
            if (filename, qualname) not in ALLOWLIST:
                found.append(f"{filename}:{qualname} mixes no_route_available with waiting_human")
    # Module level code (constants, tables) outside any function.
    function_lines = {
        line for _name, scope in scopes for line in range(scope.lineno, (scope.end_lineno or scope.lineno) + 1)
    }
    for node in ast.iter_child_nodes(tree):
        if getattr(node, "lineno", None) in function_lines or isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        if _mentions_no_route(node) and _mentions_waiting_human(node):
            found.append(f"{filename}:<module> mixes no_route_available with waiting_human")
    # A waiting_human StageResult whose cause names a route is the same bug under another spelling.
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _names(node.func)[-1:] == ["StageResult"]):
            continue
        outcome = next((k.value for k in node.keywords if k.arg == "outcome"), None)
        if not (isinstance(outcome, ast.Constant) and outcome.value == "waiting_human"):
            continue
        cause = next((k.value for k in node.keywords if k.arg == "cause_code"), None)
        if cause is None:
            continue
        if any("route" in text.lower() for text in _literals(cause)) or any("ROUTE" in n for n in _names(cause)):
            enclosing = [(s.lineno, q) for q, s in scopes if s.lineno <= node.lineno <= (s.end_lineno or s.lineno)]
            owner = max(enclosing)[1] if enclosing else "<module>"
            if (filename, owner) not in ALLOWLIST:
                found.append(f"{filename}:{owner} returns waiting_human for a route (line {node.lineno})")
    return sorted(set(found))


def _violations() -> list[str]:
    found: list[str] = []
    for path in _line_files():
        found.extend(_violations_in(path.name, path.read_text(encoding="utf-8")))
    cloud_worker_path = Path(__file__).resolve().parents[2] / "core" / "orchestrator" / "cloud_worker.py"
    found.extend(_violations_in("cloud_worker.py", cloud_worker_path.read_text(encoding="utf-8")))
    return sorted(set(found))


def test_no_route_never_parks_the_run_outside_the_post_budget_path() -> None:
    violations = _violations()
    assert not violations, (
        "A missing route must be `retry` with `not_before` (RouteWaiter.no_route_result), never "
        "`waiting_human` before the run's wall-clock budget:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize(
    "source",
    [
        # The exact pre-USR-87 code.
        'def run():\n    return StageResult(outcome="waiting_human", cause_code="no_route_available", output_refs=[])\n',
        # Same bug through a constant instead of a literal.
        'def run():\n    return StageResult(outcome="waiting_human", cause_code=NO_ROUTE_CAUSE)\n',
        # Same bug split across statements of one function.
        'def run(route):\n    if route is None:\n        cause = "no_route_available"\n        return StageResult(outcome="waiting_human", cause_code=cause)\n',
        # A different spelling of the cause that still names the route.
        'def run():\n    return StageResult(outcome="waiting_human", cause_code="no route for stage")\n',
    ],
)
def test_the_scan_catches_the_old_pattern(source: str) -> None:
    """Guard the guard: the pre-USR-87 shapes are flagged by the same scan the real files go through."""
    assert _violations_in("stage_example.py", source), source


def test_the_scan_accepts_a_retry_with_not_before() -> None:
    source = (
        'def run():\n'
        '    return StageResult(outcome="retry", cause_code="no_route_available not_before=2026-01-01T00:00:00+00:00")\n'
    )
    assert _violations_in("stage_example.py", source) == []


def test_allowlisted_scopes_exist_and_are_justified() -> None:
    for (filename, qualname), reason in ALLOWLIST.items():
        tree = ast.parse((LINE_DIR / filename).read_text(encoding="utf-8"))
        assert qualname in {q for q, _s in _scopes(tree)}, f"stale allowlist entry {filename}:{qualname}"
        assert len(reason) > 40, "an allowlist entry needs a real justification"


def test_the_post_budget_path_is_only_reachable_through_the_wall_clock_check() -> None:
    """`_park_on_infra` has exactly one caller, inside the budget branch of `no_route_result`."""
    source = (LINE_DIR / "route_wait.py").read_text(encoding="utf-8")
    callers = [
        q
        for q, scope in _scopes(ast.parse(source))
        if any(isinstance(n, ast.Attribute) and n.attr == "_park_on_infra" for n in ast.walk(scope))
    ]
    assert callers == ["RouteWaiter.no_route_result"]
    assert "cap - self.margin" in source


@pytest.mark.parametrize("module", AGENT_STAGE_MODULES)
def test_agent_stages_route_a_missing_route_through_the_waiter(module: str) -> None:
    source = (LINE_DIR / module).read_text(encoding="utf-8")
    assert "RouteWaiter" in source, f"{module} must hand 'no route' to core.line.route_wait.RouteWaiter"
    assert "no_route_result" in source, f"{module} never asks the waiter what to return"


def test_other_modules_that_pick_a_route_are_explicitly_accounted_for() -> None:
    """A new module calling `pick(` must either use the waiter or be added (with a reason) above."""
    pickers = []
    for path in _line_files():
        if path.name in AGENT_STAGE_MODULES or path.name in ("routing.py", "agent_retry.py", "route_wait.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call) and _names(node.func)[-1:] in (["pick"], ["pick_func"])
            for node in ast.walk(tree)
        ):
            pickers.append(path.name)
    unexplained = sorted(set(pickers) - set(NOT_A_ROUTE_PARK))
    assert not unexplained, (
        f"{unexplained} call pick(): use RouteWaiter.no_route_result for 'no route', or document why "
        "not in NOT_A_ROUTE_PARK"
    )


def test_cloud_worker_routes_missing_harness_through_waiter() -> None:
    source = (Path(__file__).resolve().parents[2] / "core" / "orchestrator" / "cloud_worker.py").read_text(encoding="utf-8")
    assert "RouteWaiter" in source, "cloud_worker.py must route missing harness through RouteWaiter"
    assert "no_route_result" in source, "cloud_worker.py must call RouteWaiter.no_route_result"
