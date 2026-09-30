"""T1/T2 (SPEC "Transformar o menu superior em sidebar vertical"): tests for extracting
the ~15 header action triggers of DarkHub into a fixed left sidebar.

T1 covers:
  (a) hub/frontend/index.html has a single <aside id="darkhub-sidebar"> with a
      vertical <nav> (fixed/left-0/h-screen/flex-col classes) placed before <main>;
  (b) every migrated navigation handler is still present exactly once in the file;
  (c) the <header> keeps only brand/coverage badge, project selector, status badges
      and the Ctrl+K search trigger -- no migrated navigation button remains inside it;
  (d) the topbar (<header>) and <main> apply the sidebar content offset (md:pl-56).

T2 covers:
  (e) every hand-authored utility class used by the sidebar/topbar layout has a
      matching definition in hub/frontend/styles.css;
  (f) hub/frontend/styles.css and hub/frontend/static/styles.css stay byte-identical
      (same sha256 and size), both above 10 KB, and /static/styles.css serves 200;
  (g) the collapsed-sidebar state (hidden labels, w-16 width) and the mobile overlay
      backdrop are expressed as plain CSS rules, with no @import/CDN added.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
STYLES_CSS = FRONTEND_DIR / "styles.css"
STATIC_STYLES_CSS = FRONTEND_DIR / "static" / "styles.css"

SIDEBAR_UTILITY_CLASSES = [
    "fixed",
    "left-0",
    "top-0",
    "h-screen",
    "w-56",
    "w-16",
    "flex-col",
    "overflow-y-auto",
    "z-40",
    "z-50",
    "transition-transform",
    "-translate-x-full",
    "translate-x-0",
    "md:pl-56",
    "md:pl-16",
]


def _read_css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


def _assert_utility_class_defined(css: str, class_name: str) -> None:
    selector = "." + class_name.replace(":", r"\:")
    pattern = re.escape(selector) + r"(?![\w-])"
    assert re.search(pattern, css), (
        f"expected styles.css to define a '{selector}' rule for utility class '{class_name}'"
    )

HANDLERS = [
    "openBenchmarksModal()",
    "openLearningDrawer()",
    "openPortfolioDrawer()",
    "openRoadmapDrawer()",
    "openDemandsDrawer()",
    "openTelemetryDrawer()",
    "openPlaygroundDrawer()",
    "openPromptVaultDrawer()",
    "openBackupModal()",
    "openServiceModal()",
]

SCROLL_TARGETS = [
    "content-studio-section",
    "harness-validation-section",
    "infrastructure-cards-section",
    "tasks-dashboard-section",
]


def _read_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _extract_tag_block(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>.*?</{tag}>", html, re.DOTALL)
    assert match, f"expected to find a single <{tag}>...</{tag}> block"
    return match.group(0)


def test_sidebar_aside_exists_with_vertical_layout_classes():
    html = _read_html()

    aside_matches = re.findall(r'<aside[^>]*id="darkhub-sidebar"[^>]*>', html)
    assert len(aside_matches) == 1, "expected exactly one <aside id=\"darkhub-sidebar\">"

    aside_open_tag = aside_matches[0]
    for cls in ("fixed", "left-0", "top-0", "h-screen", "w-56", "flex-col"):
        assert cls in aside_open_tag, f"<aside id=darkhub-sidebar> must include the '{cls}' utility class"

    aside_block = _extract_tag_block(html, "aside")
    assert "<nav" in aside_block, "darkhub-sidebar must contain a <nav> element"

    aside_pos = html.index('<aside')
    main_pos = html.index('<main')
    assert aside_pos < main_pos, "the sidebar <aside> must appear before <main> in document order"


def test_all_navigation_handlers_preserved_exactly_once():
    html = _read_html()

    for handler in HANDLERS:
        count = html.count(handler)
        assert count == 1, f"expected handler '{handler}' exactly once, found {count}"

    for target in SCROLL_TARGETS:
        pattern = rf"document\.getElementById\(['\"]{re.escape(target)}['\"]\)\?\.scrollIntoView"
        matches = re.findall(pattern, html)
        assert len(matches) == 1, (
            f"expected scrollIntoView trigger for '{target}' exactly once, found {len(matches)}"
        )


def test_header_keeps_only_brand_project_status_and_search():
    html = _read_html()
    header_block = _extract_tag_block(html, "header")

    for handler in HANDLERS:
        assert handler not in header_block, f"header must not contain migrated handler '{handler}'"

    for target in SCROLL_TARGETS:
        assert target not in header_block, f"header must not contain migrated scroll target '{target}'"

    for required in (
        "global-project-select",
        "ollama-status-badge",
        "openrouter-status-badge",
        "openCommandPalette()",
        "hub-coverage-badge",
    ):
        assert required in header_block, f"header must still contain '{required}'"


def test_topbar_and_main_apply_sidebar_offset():
    html = _read_html()

    header_open_tag = re.search(r"<header\b[^>]*>", html)
    assert header_open_tag, "expected a <header> tag"
    assert "md:pl-56" in header_open_tag.group(0), "<header> must apply the md:pl-56 sidebar offset"

    main_open_tag = re.search(r"<main\b[^>]*>", html)
    assert main_open_tag, "expected a <main> tag"
    assert "md:pl-56" in main_open_tag.group(0), "<main> must apply the md:pl-56 sidebar offset"


def test_index_route_still_serves_restructured_html():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="darkhub-sidebar"' in response.text


def test_sidebar_utility_classes_defined_in_styles_css():
    css = _read_css()

    for class_name in SIDEBAR_UTILITY_CLASSES:
        _assert_utility_class_defined(css, class_name)

    assert "@import" not in css, "styles.css must stay self-contained (no @import)"
    assert "cdn." not in css.lower(), "styles.css must not reference external CDNs"


def test_styles_css_and_static_copy_are_byte_identical():
    css_bytes = STYLES_CSS.read_bytes()
    static_css_bytes = STATIC_STYLES_CSS.read_bytes()

    assert len(css_bytes) == len(static_css_bytes), (
        "hub/frontend/styles.css and hub/frontend/static/styles.css must be the same size"
    )
    assert hashlib.sha256(css_bytes).hexdigest() == hashlib.sha256(static_css_bytes).hexdigest(), (
        "hub/frontend/styles.css and hub/frontend/static/styles.css must be byte-identical"
    )
    assert len(css_bytes) > 10_000, "hub/frontend/styles.css must stay above 10 KB"
    assert len(static_css_bytes) > 10_000, "hub/frontend/static/styles.css must stay above 10 KB"

    client = TestClient(app)
    response = client.get("/static/styles.css")
    assert response.status_code == 200


def test_collapsed_state_rules_present_in_css():
    css = _read_css()

    label_hidden_pattern = r"\.sidebar-collapsed\s+\.sidebar-label\s*\{[^}]*display:\s*none"
    assert re.search(label_hidden_pattern, css), (
        "expected a '.sidebar-collapsed .sidebar-label { display: none }' rule "
        "so collapsing the sidebar hides labels via CSS alone"
    )

    collapsed_width_pattern = r"\.sidebar-collapsed[^{]*#darkhub-sidebar\s*\{[^}]*width:\s*4rem"
    assert re.search(collapsed_width_pattern, css), (
        "expected a collapsed-state rule shrinking #darkhub-sidebar to the w-16 (4rem) width"
    )

    backdrop_pattern = r"\.sidebar-backdrop\s*\{[^}]*position:\s*fixed"
    assert re.search(backdrop_pattern, css), (
        "expected a '.sidebar-backdrop' rule for the mobile overlay backdrop"
    )

    preexisting_last_media_block = css.rindex("@media (prefers-reduced-motion: reduce)")
    assert css.index(".sidebar-collapsed") > preexisting_last_media_block, (
        "collapsed-state rules must be grouped after the existing @media blocks, "
        "at the end of the file"
    )
