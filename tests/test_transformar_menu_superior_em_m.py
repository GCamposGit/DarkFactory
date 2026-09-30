"""T1 (SPEC "Transformar o menu superior em sidebar vertical"): tests for extracting
the ~15 header action triggers of DarkHub into a fixed left sidebar.

Covers:
  (a) hub/frontend/index.html has a single <aside id="darkhub-sidebar"> with a
      vertical <nav> (fixed/left-0/h-screen/flex-col classes) placed before <main>;
  (b) every migrated navigation handler is still present exactly once in the file;
  (c) the <header> keeps only brand/coverage badge, project selector, status badges
      and the Ctrl+K search trigger -- no migrated navigation button remains inside it;
  (d) the topbar (<header>) and <main> apply the sidebar content offset (md:pl-56).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

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
