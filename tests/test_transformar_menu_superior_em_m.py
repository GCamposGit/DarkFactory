"""T1/T2/T3 (Transformar menu superior em menu lateral): CSS, markup and behavior tests.

Covers:
  (a) scripts/generate_hub_styles.py emits the .dh-* sidebar component layer;
  (b) hub/frontend/styles.css and hub/frontend/static/styles.css are byte-identical
      mirrors, contain the .dh-* selectors, and /static/styles.css is served
      correctly as text/css;
  (c) the sidebar CSS has the responsive off-canvas collapse below 1024px and
      respects prefers-reduced-motion;
  (d) hub/frontend/index.html wraps the page in .dh-shell with a single
      #dh-sidebar containing the 12 navigation links, a slimmed-down <header>
      without navigation buttons, an accessible toggle/overlay pair, and all
      pre-existing section/badge ids preserved exactly once;
  (e) hub/frontend/sidebar.js implements the off-canvas toggle/overlay/Escape
      behavior and active-route marking (hash + IntersectionObserver), shares
      the same script cache token as the other <script> tags, and introduces
      no external dependencies or stray globals.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import lxml.html
from fastapi.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
STYLES_CSS = FRONTEND_DIR / "styles.css"
STATIC_STYLES_CSS = FRONTEND_DIR / "static" / "styles.css"
SCRIPTS_DIR = REPO_ROOT / "scripts"
SIDEBAR_JS = FRONTEND_DIR / "sidebar.js"

_SCRIPT_SRC_TAG_RE = re.compile(r'<script[^>]*\bsrc=["\']([^"\']+)["\']')
_CACHE_TOKEN_RE = re.compile(r"\?v=([^&\"']+)")

EXPECTED_NAV_LABELS = [
    "Benchmarks",
    "Aprendizado",
    "Portfólio",
    "Roadmap",
    "Demandas",
    "Telemetria",
    "Estúdio",
    "Testes",
    "Infra",
    "Tarefas",
    "Playground",
    "Prompts",
]

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


# ---------------------------------------------------------------------------
# T2: index.html markup tests (topbar + vertical sidebar restructure)
# ---------------------------------------------------------------------------

def _class_list(el) -> list[str]:
    return (el.get("class") or "").split()


def _load_doc():
    html = INDEX_HTML.read_text(encoding="utf-8")
    return lxml.html.fromstring(html)


def test_sidebar_markup_exists_with_navigation_links():
    doc = _load_doc()

    asides = doc.xpath('//aside[@id="dh-sidebar"]')
    assert len(asides) == 1, "expected exactly one <aside id=\"dh-sidebar\">"
    sidebar = asides[0]
    assert "dh-sidebar" in _class_list(sidebar)

    navs = sidebar.xpath('.//nav[@aria-label]')
    assert len(navs) == 1, "sidebar must contain exactly one <nav> with an aria-label"
    aria_label = navs[0].get("aria-label", "")
    assert aria_label.strip(), "sidebar <nav> must have a non-empty aria-label"

    nav_links = navs[0].xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " dh-nav-link ")]'
    )
    assert len(nav_links) == 12, f"expected 12 .dh-nav-link items, found {len(nav_links)}"

    link_texts = [" ".join(link.itertext()).strip() for link in nav_links]
    for label in EXPECTED_NAV_LABELS:
        assert any(label in text for text in link_texts), (
            f"expected a .dh-nav-link labelled '{label}', got {link_texts}"
        )


def test_topbar_no_longer_holds_navigation_buttons():
    doc = _load_doc()

    headers = doc.xpath("//header")
    assert len(headers) == 1
    header = headers[0]

    header_nav_links = header.xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " dh-nav-link ")]'
    )
    assert header_nav_links == [], "header must not contain any .dh-nav-link"

    header_text = " ".join(header.itertext())
    for label in EXPECTED_NAV_LABELS:
        assert label not in header_text, (
            f"navigation label '{label}' must have been moved out of <header>"
        )

    assert header.xpath('.//select[@id="global-project-select"]'), (
        "header must keep the project selector"
    )
    assert "Buscar" in header_text, "header must keep the Ctrl+K search trigger"
    assert "Novo" in header_text, "header must keep the 'Novo' button"
    assert header.xpath('.//button[@onclick="openBackupModal()"]'), (
        "header must keep the Backup & Configurações (⚙️) trigger"
    )


def test_sidebar_links_preserve_original_handlers():
    doc = _load_doc()
    sidebar = doc.xpath('//aside[@id="dh-sidebar"]')[0]
    nav_links = sidebar.xpath(
        './/nav//*[contains(concat(" ", normalize-space(@class), " "), " dh-nav-link ")]'
    )
    assert len(nav_links) == 12

    call_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)$")
    for link in nav_links:
        onclick = (link.get("onclick") or "").strip()
        assert onclick, f"nav link {link.get('title')!r} must keep a non-empty onclick"
        is_direct_call = bool(call_pattern.match(onclick))
        is_scroll_into_view = (
            "document.getElementById(" in onclick and "scrollIntoView" in onclick
        )
        assert is_direct_call or is_scroll_into_view, (
            f"unexpected onclick handler shape: {onclick!r}"
        )

        title = (link.get("title") or "").strip()
        assert title, f"nav link with onclick {onclick!r} must keep its original title"

    portfolio_links = sidebar.xpath('.//*[@id="portfolio-trigger-btn"]')
    assert len(portfolio_links) == 1, "the #portfolio-trigger-btn id must be preserved"
    assert portfolio_links[0].get("onclick") == "openPortfolioDrawer()"


def test_sidebar_toggle_and_overlay_accessibility():
    doc = _load_doc()

    toggles = doc.xpath('//*[@id="dh-sidebar-toggle"]')
    assert len(toggles) == 1
    toggle = toggles[0]
    assert toggle.get("aria-controls") == "dh-sidebar"
    assert toggle.get("aria-expanded") == "false"
    assert (toggle.get("aria-label") or "").strip(), (
        "sidebar toggle must have an accessible label"
    )

    overlays = doc.xpath('//*[@id="dh-sidebar-overlay"]')
    assert len(overlays) == 1
    overlay = overlays[0]
    assert "is-open" not in _class_list(overlay), "overlay must be inert by default"
    assert overlay.get("aria-hidden") == "true"
    assert overlay.text_content().strip() == "", "overlay must not carry interactive content"


def test_existing_sections_and_badge_ids_preserved():
    doc = _load_doc()

    critical_ids = [
        "global-project-select",
        "ollama-status-badge",
        "openrouter-status-badge",
        "hub-coverage-badge",
        "tasks-dashboard-section",
        "quick-dock-section",
    ]
    for critical_id in critical_ids:
        matches = doc.xpath(f'//*[@id="{critical_id}"]')
        assert len(matches) == 1, f"#{critical_id} must remain unique in the document"

    main_sections = [
        "interventions-hero-strip",
        "line-status-card",
        "quick-dock-section",
        "tasks-dashboard-section",
        "services-grid",
        "empty-state",
    ]
    main_elements = doc.xpath("//main")
    assert len(main_elements) == 1
    main_el = main_elements[0]
    for section_id in main_sections:
        assert main_el.xpath(f'.//*[@id="{section_id}"]'), (
            f"#{section_id} must remain inside <main>"
        )

    dh_main = doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " dh-main ")]')
    assert len(dh_main) == 1
    assert dh_main[0].xpath(".//main"), "<main> must live inside .dh-main"

    # Document-wide tag counts: exactly one sidebar <nav>/<main>, and the two
    # expected <aside> elements (#dh-sidebar plus the pre-existing
    # #roadmap-detail aside inside the Roadmap drawer, out of scope for T2).
    assert len(doc.xpath("//nav")) == 1
    assert len(doc.xpath("//main")) == 1
    assert len(doc.xpath("//aside")) == 2


# ---------------------------------------------------------------------------
# T3: sidebar.js behavior tests (toggle/overlay/Escape + active-route marking)
# ---------------------------------------------------------------------------

def test_sidebar_script_loaded_with_shared_cache_version():
    assert SIDEBAR_JS.is_file(), f"{SIDEBAR_JS} must exist on disk"

    html = INDEX_HTML.read_text(encoding="utf-8")
    script_srcs = _SCRIPT_SRC_TAG_RE.findall(html)
    assert script_srcs, "expected at least one <script src=...> tag in index.html"

    def _version(src: str) -> str:
        match = _CACHE_TOKEN_RE.search(src)
        assert match, f"script src {src!r} must carry a ?v= cache token"
        return match.group(1)

    versions = {_version(src) for src in script_srcs}
    assert len(versions) == 1, f"all <script> tags must share a single cache token, found {versions}"

    sidebar_srcs = [src for src in script_srcs if "/sidebar.js" in src]
    assert len(sidebar_srcs) == 1, "sidebar.js must be referenced by exactly one <script> tag"
    assert sidebar_srcs[0] == f"/static/sidebar.js?v={next(iter(versions))}", (
        "sidebar.js must be loaded with the same cache token as the other scripts"
    )


def test_sidebar_script_implements_toggle_overlay_and_escape():
    source = SIDEBAR_JS.read_text(encoding="utf-8")

    assert "dh-sidebar-toggle" in source, "script must reference the sidebar toggle button"
    assert "dh-sidebar-overlay" in source, "script must reference the sidebar overlay"
    assert re.search(r'["\']Escape["\']', source), "script must handle the Escape key"
    assert "aria-expanded" in source, "script must sync aria-expanded on the toggle"
    assert "is-open" in source, "script must toggle the .is-open state class"

    assert re.search(r"toggle\.addEventListener\(\s*[\"']click[\"']", source), (
        "script must bind a click handler on the toggle"
    )
    assert re.search(r"overlay\.addEventListener\(\s*[\"']click[\"']", source), (
        "script must bind a click handler on the overlay"
    )
    assert re.search(r"addEventListener\(\s*[\"']keydown[\"']", source), (
        "script must listen for keydown to detect Escape"
    )


def test_sidebar_script_marks_active_route():
    source = SIDEBAR_JS.read_text(encoding="utf-8")

    assert "aria-current" in source, "script must mark the active link with aria-current"
    assert "hashchange" in source, "script must react to location.hash changes"
    assert "IntersectionObserver" in source, (
        "script must track the visible <main> section via IntersectionObserver"
    )

    for forbidden in ("fetch(", "XMLHttpRequest", "/api/"):
        assert forbidden not in source, (
            f"sidebar.js must not call application APIs (found {forbidden!r})"
        )


def test_sidebar_script_has_no_external_dependencies_or_globals():
    source = SIDEBAR_JS.read_text(encoding="utf-8")

    for forbidden in ("http://", "https://", "import ", "require("):
        assert forbidden not in source, (
            f"sidebar.js must have no external dependencies (found {forbidden!r})"
        )

    assert source.lstrip().startswith("//"), "sidebar.js should open with an explanatory comment"
    assert re.search(r"\(function\s*\(\s*\)\s*{", source), (
        "sidebar.js must be wrapped in an IIFE to avoid leaking locals"
    )

    window_assignments = sorted(set(re.findall(r"\bwindow\.(\w+)\s*=", source)))
    assert len(window_assignments) <= 1, (
        f"sidebar.js must expose at most one global namespace, found {window_assignments}"
    )
