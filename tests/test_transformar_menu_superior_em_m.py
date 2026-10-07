"""T1 (Transformar menu superior em menu lateral): CSS component layer tests.

Covers:
  (a) scripts/generate_hub_styles.py emits the .dh-* sidebar component layer;
  (b) hub/frontend/styles.css and hub/frontend/static/styles.css are byte-identical
      mirrors, contain the .dh-* selectors, and /static/styles.css is served
      correctly as text/css;
  (c) the sidebar CSS has the responsive off-canvas collapse below 1024px and
      respects prefers-reduced-motion.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
STYLES_CSS = FRONTEND_DIR / "styles.css"
STATIC_STYLES_CSS = FRONTEND_DIR / "static" / "styles.css"
SCRIPTS_DIR = REPO_ROOT / "scripts"

DH_SELECTORS = [
    ".dh-shell",
    ".dh-main",
    ".dh-sidebar",
    ".dh-sidebar-brand",
    ".dh-sidebar-nav",
    ".dh-nav-link",
    '.dh-nav-link[aria-current="page"]',
    ".dh-sidebar-footer",
    ".dh-sidebar-toggle",
    ".dh-sidebar-overlay",
]


def _load_generator():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    module = importlib.import_module("generate_hub_styles")
    return importlib.reload(module)


def _extract_at_rule_block(css: str, at_rule: str, after: int = 0) -> str:
    """Returns the full, brace-balanced body of the next `at_rule { ... }` block found after `after`."""
    start = css.index(at_rule, after)
    brace_open = css.index("{", start)
    depth = 0
    for i, ch in enumerate(css[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return css[brace_open + 1 : i]
    raise AssertionError(f"unbalanced braces for at-rule {at_rule!r}")


def test_generator_emits_sidebar_component_layer():
    generator = _load_generator()
    css = generator.build_stylesheet()
    for selector in DH_SELECTORS:
        assert selector in css, f"generator output must include selector {selector!r}"


def test_sidebar_css_is_mirrored_and_served():
    assert STYLES_CSS.is_file(), f"{STYLES_CSS} must exist on disk"
    assert STATIC_STYLES_CSS.is_file(), f"{STATIC_STYLES_CSS} must exist on disk"

    primary = STYLES_CSS.read_text(encoding="utf-8")
    mirror = STATIC_STYLES_CSS.read_text(encoding="utf-8")
    assert primary == mirror, "styles.css and static/styles.css must be byte-identical"

    for selector in DH_SELECTORS:
        assert selector in primary, f"styles.css must include selector {selector!r}"

    client = TestClient(app)
    response = client.get("/static/styles.css")
    assert response.status_code == 200, f"Expected 200 from /static/styles.css, got {response.status_code}"
    assert "text/css" in response.headers.get("content-type", "").lower()


def test_sidebar_css_has_responsive_collapse_and_reduced_motion():
    css = STYLES_CSS.read_text(encoding="utf-8")

    shell_offset = css.index(".dh-shell")

    assert "@media (max-width: 1023.98px)" in css[shell_offset:], (
        "sidebar CSS must define the off-canvas collapse breakpoint at 1023.98px"
    )
    collapse_block = _extract_at_rule_block(css, "@media (max-width: 1023.98px)", after=shell_offset)
    assert "translateX(-100%)" in collapse_block, (
        "sidebar must translate off-canvas by default below 1024px"
    )
    assert ".dh-sidebar.is-open" in collapse_block, (
        "sidebar must become visible via the .is-open state below 1024px"
    )
    assert ".dh-sidebar-overlay.is-open" in collapse_block, (
        "overlay must only appear paired with the .is-open state"
    )

    assert "@media (prefers-reduced-motion: reduce)" in css[shell_offset:]
    reduced_motion_block = _extract_at_rule_block(
        css, "@media (prefers-reduced-motion: reduce)", after=shell_offset
    )
    assert ".dh-sidebar" in reduced_motion_block
    assert ".dh-sidebar-overlay" in reduced_motion_block
    assert "transition: none" in reduced_motion_block
