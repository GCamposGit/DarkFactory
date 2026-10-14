"""Static scanner for the "clock bomb" test defect (USR-239).

Background
----------
On 2026-10-11 the full suite failed on every check (Ubuntu and Windows) from 15:00 UTC on:
``tests/test_line_live.py::test_api_line_live_includes_local_runs`` raised ``StopIteration`` because
``RECENT_RUN_WINDOW`` (``core/workflow/line_live.py``) is 7 days, the test's seed was anchored at a
fixed ``2026-10-04 15:00 UTC``, and the test read the real wall clock instead of pinning it. Main and
every PR stayed red for ~3 hours. PR 302 fixed it with an autouse fixture that monkeypatches
``core.workflow.line_live._utc_now``; an earlier, partial fix (PR 264) had missed this exact test.

USR-239 audited every other test file the same defect could lurk in (listed in the ticket's
``problem_statement``: ``test_artifact_retention``, ``test_cloud_worker_active_loop``,
``test_control_store_failure_evidence``, ``test_line_acceptance``, ``test_line_dogfood``,
``test_line_live``, ``test_line_owner_intake_expired``, ``test_local_progress_visibility``,
``test_local_progress``, ``test_postgres_real_integration``, ``test_run_ticket_cancel``,
``test_telegram_status_usr197``). Result: only ``test_line_live.py`` had the live defect (fixed by
PR 302, see its ``_pin_line_live_clock`` autouse fixture). Every other file in that list already
threads its fixed 2026 date explicitly into the ``now=``/``clock=`` parameter of the production code
it exercises, so the production code never falls back to the real clock while running a window/TTL/age
comparison against that fixed date.

This module is the blunt, static instrument USR-239's criterion 3 asks for, to catch a *future*
regression of the same class in seconds, without running the suite:

    a test file creates a fixed 2026 date AND does not fix the clock in the same file.

It is deliberately not semantic: it cannot tell whether the fixed date is ever actually compared
against a real-clock read several call frames deep in production code. That is exactly why
``CLOCK_BOMB_EXCEPTIONS`` exists -- a file lands there, with a one-line reason, once a human (or this
ticket's audit) has actually traced the code paths and confirmed there is no live dependency on the
execution date. A new file that mixes a fixed 2026 date with un-pinned real-clock code must either pin
the clock in the same file or earn its own reasoned entry here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Any Call keyword argument whose name has a "_"-separated TOKEN equal to one of these markers, anywhere
# in the file, counts as "the clock is fixed in this file" (token match, so `known=`/`snow=` do not count) -- the file hands the production code an explicit instant/factory
# instead of letting it fall back to the real clock. Matches the ticket's "now= passado explicitamente"
# and covers the DI variants actually used in this repo (`now_fn=`, `clock=`, `observed_at=NOW`, ...).
_CLOCK_KEYWORD_ARG_MARKERS = ("now", "clock")

# monkeypatch.setattr(target, "<attr>", ...) / mock.patch.object(target, "<attr>", ...) counts as
# "fixes the clock" when <attr> looks like a clock seam (e.g. "_utc_now", "_now", "clock").
_CLOCK_ATTR_NAME_MARKERS = ("now", "clock")


def _name_has_clock_token(name: str, markers: tuple[str, ...]) -> bool:
    """True when some `_`-separated, lowercased part of `name` is exactly one of `markers`."""
    return any(part in markers for part in name.lower().split("_"))

# Literal substrings whose presence anywhere in the source counts as a freezer (freezegun).
_FREEZER_MARKERS = ("freeze_time", "freezegun")


@dataclass(frozen=True)
class ClockBombHit:
    path: str  # POSIX, relative to the repo root
    line: int
    detail: str


def _is_fixed_2026_date_call(node: ast.AST) -> str | None:
    """Return a short description if `node` is a `datetime(2026, ...)` / `date(2026, ...)` call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name not in {"datetime", "date"}:
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and first.value == 2026:
        return f"{name}(2026, ...)"
    return None


def _is_fixed_2026_iso_string(node: ast.AST) -> str | None:
    """Return a short description if `node` is a string constant containing an ISO '2026-' date."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "2026-" in node.value:
        return repr(node.value) if len(node.value) <= 40 else repr(node.value[:37] + "...")
    return None


def _call_has_clock_keyword(node: ast.Call) -> bool:
    return any(
        kw.arg is not None and _name_has_clock_token(kw.arg, _CLOCK_KEYWORD_ARG_MARKERS)
        for kw in node.keywords
    )


def _setattr_target_attr_name(node: ast.Call) -> str | None:
    """For a `monkeypatch.setattr(obj, "attr", value)` / `mock.patch.object(obj, "attr", ...)` call,
    return the literal attribute-name string, if any."""
    func = node.func
    func_name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    if func_name not in {"setattr", "patch", "object"}:
        return None
    if len(node.args) < 2:
        return None
    attr_arg = node.args[1]
    if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
        return attr_arg.value
    return None


def _file_fixes_the_clock(tree: ast.Module, source: str) -> bool:
    if any(marker in source for marker in _FREEZER_MARKERS):
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_has_clock_keyword(node):
            return True
        attr_name = _setattr_target_attr_name(node)
        if attr_name is not None:
            if _name_has_clock_token(attr_name, _CLOCK_ATTR_NAME_MARKERS):
                return True
    return False


def find_fixed_2026_dates(tree: ast.Module) -> list[tuple[int, str]]:
    """Every fixed-2026-date literal in `tree`, as `(line, detail)`, in source order."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        detail = _is_fixed_2026_date_call(node) or _is_fixed_2026_iso_string(node)
        if detail is not None:
            hits.append((node.lineno, detail))
    hits.sort(key=lambda item: item[0])
    return hits


def scan_file(path: Path, source: str) -> ClockBombHit | None:
    """Return the first clock-bomb hit in this file's source, or None if it is clean."""
    if "2026" not in source:
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    hits = find_fixed_2026_dates(tree)
    if not hits:
        return None
    if _file_fixes_the_clock(tree, source):
        return None
    line, detail = hits[0]
    return ClockBombHit(path=path.as_posix(), line=line, detail=detail)


def scan_tests_tree(tests_root: Path, *, repo_root: Path) -> list[ClockBombHit]:
    hits: list[ClockBombHit] = []
    for py_file in sorted(tests_root.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        hit = scan_file(py_file.relative_to(repo_root), source)
        if hit is not None:
            hits.append(hit)
    return hits
