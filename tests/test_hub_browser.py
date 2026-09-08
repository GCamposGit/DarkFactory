"""
Tests for DF-07: Protection against XSS, malicious protocols, colors, and IDs in DarkHub.
Covers Pydantic backend models and browser/frontend rendering inertness.
"""

import re
from pathlib import Path
import pytest
from pydantic import ValidationError
import lxml.html

from hub.backend.models import (
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    ServiceUpdate,
    ImportCatalogRequest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"


# ---------------------------------------------------------------------------
# Backend Pydantic Model Validation Tests (Protocols, Colors, IDs)
# ---------------------------------------------------------------------------

def test_service_models_reject_dangerous_protocols():
    """Verify that dangerous URL schemes like javascript:, data:, file:, vbscript: are rejected."""
    dangerous_urls = [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "file:///C:/Windows/System32/calc.exe",
        "ftp://attacker.com/malware",
        "gopher://attacker.com/something",
        "//attacker.com/relative-proto",
        "http://",  # missing netloc
        "https://",  # missing netloc
        "http://\x00malicious.com",  # null byte
        "http://malicious.com\r\nHeader: Injected",  # CRLF
    ]

    for bad_url in dangerous_urls:
        with pytest.raises(ValidationError, match="Invalid URL protocol|URL must contain|illegal control"):
            ServiceCreate(
                name="Malicious Service",
                url=bad_url,
                category=ServiceCategory.CUSTOM,
            )

        with pytest.raises(ValidationError):
            ServiceItem(
                id="safe-id",
                name="Malicious Service",
                url=bad_url,
                category=ServiceCategory.CUSTOM,
            )

        with pytest.raises(ValidationError):
            ServiceUpdate(url=bad_url)


def test_service_models_accept_legitimate_http_and_https_urls():
    """Verify that valid http:// and https:// URLs pass validation cleanly."""
    valid_urls = [
        "http://localhost:11434",
        "http://127.0.0.1:8888",
        "https://claude.ai/",
        "https://chatgpt.com/c/123",
        "https://openrouter.ai/models?category=programming",
        "http://localhost:3000/app#dashboard",
    ]

    for good_url in valid_urls:
        item = ServiceCreate(
            name="Valid Tool",
            url=good_url,
            category=ServiceCategory.CUSTOM,
        )
        assert item.url == good_url


def test_service_models_reject_invalid_colors():
    """Verify that invalid colors, CSS injections, and quotes are rejected."""
    bad_colors = [
        "red; background:url('javascript:alert(1)')",
        "#10b981; injection",
        '" onmouseover="alert(1)',
        "<script>alert(1)</script>",
        "#12",  # too short
        "#12345",  # 5 hex digits
        "#123456789",  # 9 hex digits
        "rgb(255, 0, 0)",
        "rgba(0,0,0,1)",
        "#zzzzzz",  # non-hex
    ]

    for bad_color in bad_colors:
        with pytest.raises(ValidationError, match="Invalid color"):
            ServiceCreate(
                name="Bad Color Service",
                url="http://localhost:8000",
                color=bad_color,
            )

        with pytest.raises(ValidationError):
            ServiceItem(
                id="svc-1",
                name="Bad Color Service",
                url="http://localhost:8000",
                color=bad_color,
            )

        with pytest.raises(ValidationError):
            ServiceUpdate(color=bad_color)


def test_service_models_accept_valid_hex_colors():
    """Verify that standard 3, 4, 6, and 8 hex character color codes pass validation."""
    valid_colors = ["#fff", "#FFF", "#3b82f6", "#10B981", "#10b98122", "#aabbccdd"]

    for good_color in valid_colors:
        item = ServiceCreate(
            name="Colored Tool",
            url="https://example.com",
            color=good_color,
        )
        assert item.color == good_color


def test_service_models_reject_invalid_ids():
    """Verify that ServiceItem rejects IDs with special characters, spaces, HTML, or code."""
    bad_ids = [
        "id with spaces",
        "id' onclick='alert(1)",
        "<script>",
        'id" onerror="alert(1)',
        "../../etc/passwd",
        "id;drop table",
        "id?arg=1",
        "id#hash",
        "",  # empty
    ]

    for bad_id in bad_ids:
        with pytest.raises(ValidationError, match="Invalid ID"):
            ServiceItem(
                id=bad_id,
                name="Tool",
                url="https://example.com",
            )


def test_service_models_accept_valid_ids():
    """Verify that slug-like alphanumeric IDs with dashes and underscores are accepted."""
    valid_ids = ["ollama-local", "claude_37", "service-123", "DeepSeek_R1-distill"]

    for good_id in valid_ids:
        item = ServiceItem(
            id=good_id,
            name="Tool",
            url="https://example.com",
        )
        assert item.id == good_id


def test_catalog_import_rejects_malicious_items():
    """Verify that ImportCatalogRequest fails closed if any item in the batch is invalid."""
    malicious_item_dict = {
        "id": "bad-import",
        "name": "Evil Tool",
        "url": "javascript:fetch('//evil.com')",
        "category": "custom",
        "color": "#10b981",
    }

    with pytest.raises(ValidationError):
        ImportCatalogRequest(services=[malicious_item_dict])


# ---------------------------------------------------------------------------
# Frontend Browser Invariance & Inert Active Text Simulation Tests
# ---------------------------------------------------------------------------

def test_frontend_app_js_defines_security_and_sanitization_helpers():
    """Verify that app.js contains escapeHtml, sanitizeUrl, sanitizeColor, and sanitizeId."""
    app_js_path = FRONTEND_DIR / "app.js"
    assert app_js_path.exists(), "app.js must exist"

    content = app_js_path.read_text(encoding="utf-8")
    assert "function escapeHtml" in content
    assert "function sanitizeUrl" in content
    assert "function sanitizeColor" in content
    assert "function sanitizeId" in content
    assert "initServicesGridEvents" in content


def test_frontend_app_js_does_not_interpolate_raw_event_handlers():
    """Verify that dangerous inline onclick='...${item.id}...' or '${item.url}' patterns are removed."""
    app_js_path = FRONTEND_DIR / "app.js"
    content = app_js_path.read_text(encoding="utf-8")

    # The audit specifically flagged:
    # onclick="deleteService('${item.id}')" and onclick="toggleFavorite('${item.id}')"
    # and onclick="copyToClipboard('${item.url}', ...)"
    assert 'onclick="deleteService(\'${item.id}\')"' not in content
    assert 'onclick="toggleFavorite(\'${item.id}\')"' not in content
    assert 'onclick="togglePin(\'${item.id}\')"' not in content
    assert 'onclick="editService(\'${item.id}\')"' not in content
    assert 'onclick="copyToClipboard(\'${item.url}\'' not in content

    # Should use data-action and data-service-id / data-service-url attributes instead
    assert 'data-action="toggle-favorite"' in content
    assert 'data-action="toggle-pin"' in content
    assert 'data-action="edit-service"' in content
    assert 'data-action="delete-service"' in content
    assert 'data-action="copy-url"' in content


def test_frontend_active_payload_inertness_in_rendered_html():
    """
    Simulate frontend rendering of a service item carrying persistent XSS payloads.
    Confirm that active payloads (<script>, <img onerror>, <svg onload>) remain 100% inert
    and parse as safe text nodes rather than executable DOM elements.
    """
    # Emulate the pure JS sanitization logic implemented in app.js
    def escape_html(text: str) -> str:
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#039;")
        )

    def sanitize_url(url: str) -> str:
        if not url:
            return "#"
        trimmed = url.strip()
        if re.match(r"^https?://[^\s<>'\"`]+$", trimmed, re.IGNORECASE):
            return escape_html(trimmed)
        return "#"

    def sanitize_color(color: str, default: str = "#3b82f6") -> str:
        if not color:
            return default
        trimmed = color.strip()
        if re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", trimmed):
            return trimmed
        return default

    def sanitize_id(id_str: str) -> str:
        if not id_str:
            return ""
        return re.sub(r"[^a-zA-Z0-9_-]", "", id_str)

    # Attack payload simulating an imported / stored catalog item
    item = {
        "id": "xss-payload' onfocus='alert(1)",
        "name": "<script>alert('XSS-NAME')</script>",
        "description": "<img src=x onerror=alert('XSS-DESC')>",
        "url": "javascript:alert('XSS-URL')",
        "color": "#10b981; font-size: 100px",  # invalid color injection
        "tags": ["<svg onload=alert('XSS-TAG')>", "normal-tag"],
        "category": "custom",
    }

    safe_id = sanitize_id(item["id"])
    safe_name = escape_html(item["name"])
    safe_desc = escape_html(item["description"])
    safe_url = sanitize_url(item["url"])
    safe_color = sanitize_color(item["color"])
    initials = escape_html(item["name"][:2].upper())
    safe_tags = "".join(f"<span>#{escape_html(t)}</span>" for t in item["tags"])

    rendered_snippet = f"""
    <div class="service-card" data-service-id="{safe_id}">
      <div style="background: {safe_color}">
        <span>{initials}</span>
      </div>
      <h3>{safe_name}</h3>
      <p>{safe_desc}</p>
      <div class="tags">{safe_tags}</div>
      <a href="{safe_url}">Launch</a>
      <button data-action="copy-url" data-service-url="{safe_url}">Copy</button>
      <button data-action="delete-service" data-service-id="{safe_id}">Delete</button>
    </div>
    """

    # Parse rendered HTML with lxml
    doc = lxml.html.fragment_fromstring(rendered_snippet)

    # 1. No <script> tag exists in the DOM tree
    assert len(doc.xpath("//script")) == 0, "Active <script> tags must not be created!"

    # 2. No <img> tag exists in the DOM tree
    assert len(doc.xpath("//img")) == 0, "Active <img> tags must not be created!"

    # 3. No <svg> tag exists in the DOM tree
    assert len(doc.xpath("//svg")) == 0, "Active <svg> tags must not be created!"

    # 4. No onerror, onload, or onmouseover event handler attributes exist on ANY element
    all_elements = doc.xpath("//*")
    for el in all_elements:
        for attr in el.attrib:
            assert not attr.lower().startswith("on"), f"Dangerous inline handler '{attr}' found on element {el.tag}!"

    # 5. href attribute of <a> must not be javascript:
    a_elem = doc.xpath("//a")[0]
    assert a_elem.attrib["href"] == "#", "Dangerous URL protocol must be neutralized to '#'!"

    # 6. ID attribute must be sanitized of quotes and spaces
    assert "'" not in safe_id
    assert '"' not in safe_id
    assert " " not in safe_id
    assert "=" not in safe_id

    # 7. Style background must only contain clean color hex
    assert "font-size" not in safe_color
    assert ";" not in safe_color
