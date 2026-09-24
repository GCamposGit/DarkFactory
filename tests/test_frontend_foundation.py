"""DH-13 (USR-49): Frontend Foundation Tests.

Covers:
  (a) hub/frontend/index.html does not reference cdn.tailwindcss.com;
  (b) No external CSS resources or CDNs are loaded in runtime;
  (c) /static/styles.css exists, is served with 200, is not empty, and has representative size (> 10 KB);
  (d) All script tags share the exact cache version ?v=20260923c;
  (e) Stylesheet link uses the shared cache version ?v=20260923c;
  (f) Stylesheet contains essential utility classes (colors, layout, flex/grid, transitions).
"""

from __future__ import annotations

import re
from pathlib import Path
from fastapi.testclient import TestClient

from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
STYLES_CSS = FRONTEND_DIR / "styles.css"
STATIC_STYLES_CSS = FRONTEND_DIR / "static" / "styles.css"

_SCRIPT_SRC_TAG = re.compile(r'<script[^>]*src=[\x22\x27]([^\x22\x27]+)[\x22\x27]')
_LINK_STYLESHEET_TAG = re.compile(r'<link[^>]*rel=[\x22\x27]stylesheet[\x22\x27][^>]*href=[\x22\x27]([^\x22\x27]+)[\x22\x27]|<link[^>]*href=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*rel=[\x22\x27]stylesheet[\x22\x27]')


def test_index_html_does_not_reference_tailwind_cdn():
    """Verify that cdn.tailwindcss.com and inline tailwind.config are completely eliminated."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "cdn.tailwindcss.com" not in html, "index.html must not reference cdn.tailwindcss.com"
    assert "tailwind.config" not in html, "index.html must not include inline tailwind.config"


def test_no_external_css_cdns_loaded_in_runtime():
    """Verify that zero external CSS resources are loaded from external CDNs at runtime."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    
    # Check all <link rel="stylesheet"> hrefs
    matches = _LINK_STYLESHEET_TAG.findall(html)
    hrefs = [m[0] or m[1] for m in matches]
    assert hrefs, "expected at least one stylesheet link in index.html"
    
    for href in hrefs:
        assert not href.startswith("http://") and not href.startswith("https://") and not href.startswith("//"), (
            f"Stylesheet href '{href}' points to an external CDN, violating runtime isolation."
        )
        assert href.startswith("/static/"), f"Stylesheet href '{href}' must point to local /static/ asset."


def test_static_styles_css_exists_and_representative_size():
    """Verify that /static/styles.css exists, is served via HTTP 200, and is > 10 KB."""
    # 1. Disk existence and size checks
    assert STYLES_CSS.is_file(), f"{STYLES_CSS} must exist on disk"
    size_bytes = STYLES_CSS.stat().st_size
    assert size_bytes > 10 * 1024, f"styles.css size ({size_bytes} bytes) must be > 10 KB"

    # Also verify the static/ mirror for direct filesystem access
    assert STATIC_STYLES_CSS.is_file(), f"{STATIC_STYLES_CSS} must exist on disk"
    assert STATIC_STYLES_CSS.stat().st_size > 10 * 1024, f"static/styles.css size must be > 10 KB"

    # 2. HTTP serving via FastAPI static mount
    client = TestClient(app)
    response = client.get("/static/styles.css")
    assert response.status_code == 200, f"Expected 200 from /static/styles.css, got {response.status_code}"
    assert len(response.content) > 10 * 1024, f"HTTP served styles.css must be > 10 KB, got {len(response.content)}"
    assert "text/css" in response.headers.get("content-type", "").lower()


def test_all_script_tags_share_cache_version_20260924b():
    """Verify that every script tag in index.html shares the expected ?v=20260924b cache version."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    script_srcs = _SCRIPT_SRC_TAG.findall(html)
    assert len(script_srcs) >= 10, f"expected at least 10 script tags in index.html, found {len(script_srcs)}"

    expected_version = "20260924b"
    versions = set()
    for src in script_srcs:
        assert "?v=" in src, f"Script tag '{src}' is missing ?v= cache busting parameter"
        version = src.split("?v=")[-1]
        versions.add(version)
        assert version == expected_version, f"Script tag '{src}' has version '{version}', expected '{expected_version}'"

    assert len(versions) == 1, f"All script tags must share a single cache version, found {versions}"


def test_stylesheet_link_uses_cache_version_20260924b():
    """Verify that the stylesheet link in index.html uses ?v=20260924b."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/styles.css?v=20260924b">' in html, (
        "index.html must link styles.css with ?v=20260924b"
    )


def test_stylesheet_contains_key_utility_classes():
    """Verify that key utility classes for DarkHub UI are present in the stylesheet."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    
    # Palette colors & opacities
    expected_classes = [
        ".bg-slate-950",
        ".text-slate-100",
        ".bg-indigo-500",
        ".bg-indigo-500\\/10",
        ".text-indigo-400",
        ".border-indigo-500\\/20",
        ".bg-emerald-500\\/10",
        ".text-emerald-400",
        ".border-emerald-500\\/20",
        ".bg-amber-500\\/10",
        ".text-amber-400",
        ".bg-rose-500\\/10",
        ".text-cyan-400",
        ".flex",
        ".grid",
        ".col-span-full",
        ".rounded-xl",
        ".rounded-full",
        ".font-mono",
        ".transition-all",
        ".backdrop-blur-xl",
        ".glass-panel",
        ".glass-card",
        ".status-dot-online",
    ]
    for cls in expected_classes:
        assert cls in css, f"Expected utility class '{cls}' not found in styles.css"
