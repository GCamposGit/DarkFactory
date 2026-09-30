"""T1 (SPEC: transformar menu superior em sidebar vertical no DarkHub).

Covers:
  (a) hub/frontend/styles.css defines the new sidebar layout/component classes;
  (b) a responsive @media (max-width: ...) block covers the sidebar for small screens;
  (c) hub/frontend/styles.css and hub/frontend/static/styles.css stay byte-identical;
  (d) pre-existing critical utility classes required by test_frontend_foundation.py
      are still present (this ticket must only grow the file).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
STYLES_CSS = FRONTEND_DIR / "styles.css"
STATIC_STYLES_CSS = FRONTEND_DIR / "static" / "styles.css"

_MEDIA_MAX_WIDTH_RE = re.compile(r"@media\s*\([^)]*max-width\s*:\s*[0-9.]+(?:px|rem|em)[^)]*\)\s*\{")


def test_sidebar_css_classes_present():
    """Every new sidebar selector required by T1 must exist in styles.css."""
    css = STYLES_CSS.read_text(encoding="utf-8")

    expected_selectors = [
        ".darkhub-sidebar",
        ".darkhub-sidebar-nav",
        ".darkhub-sidebar-item",
        ".darkhub-sidebar-label",
        ".darkhub-sidebar-toggle",
        ".darkhub-sidebar-backdrop",
        ".darkhub-sidebar-collapsed",
        ".darkhub-main-offset",
    ]
    for selector in expected_selectors:
        assert selector in css, f"Expected sidebar selector '{selector}' not found in styles.css"


def test_sidebar_css_has_responsive_media_query():
    """A max-width media query must cover the sidebar for small-screen/overlay behavior."""
    css = STYLES_CSS.read_text(encoding="utf-8")

    media_blocks = _MEDIA_MAX_WIDTH_RE.findall(css)
    assert media_blocks, "Expected at least one @media (max-width: ...) block in styles.css"

    sidebar_media_match = re.search(
        r"@media\s*\([^)]*max-width\s*:\s*[0-9.]+(?:px|rem|em)[^)]*\)\s*\{[^{}]*\.darkhub-sidebar",
        css,
        re.DOTALL,
    )
    assert sidebar_media_match, (
        "Expected a @media (max-width: ...) block whose body references .darkhub-sidebar "
        "for the small-screen overlay behavior"
    )


def test_styles_css_and_static_mirror_are_byte_identical():
    """hub/frontend/styles.css and its hub/frontend/static/styles.css mirror must match exactly."""
    primary_bytes = STYLES_CSS.read_bytes()
    mirror_bytes = STATIC_STYLES_CSS.read_bytes()

    assert len(primary_bytes) == len(mirror_bytes), (
        f"Size mismatch: styles.css={len(primary_bytes)} bytes, "
        f"static/styles.css={len(mirror_bytes)} bytes"
    )
    assert hashlib.sha256(primary_bytes).hexdigest() == hashlib.sha256(mirror_bytes).hexdigest(), (
        "hub/frontend/styles.css and hub/frontend/static/styles.css must be byte-identical (SHA-256 mismatch)"
    )


def test_existing_frontend_foundation_classes_preserved():
    """Critical pre-existing utility classes (test_frontend_foundation.py) must still be present."""
    css = STYLES_CSS.read_text(encoding="utf-8")

    critical_preexisting_classes = [
        ".bg-slate-950",
        ".text-slate-100",
        ".bg-indigo-500",
        ".flex",
        ".grid",
        ".rounded-xl",
        ".rounded-full",
        ".transition-all",
        ".backdrop-blur-xl",
        ".glass-panel",
        ".glass-card",
        ".status-dot-online",
    ]
    for cls in critical_preexisting_classes:
        assert cls in css, f"Pre-existing utility class '{cls}' must be preserved in styles.css"
