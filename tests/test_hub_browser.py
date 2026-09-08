"""Browser-boundary regression tests for the DarkHub service catalog."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.backend.models import ServiceCreate, ServiceItem, ServiceUpdate


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "hub" / "frontend" / "app.js"


def _service_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "safe-service",
        "name": "Safe service",
        "url": "https://example.com/tool",
        "description": "Safe description",
        "tags": ["safe"],
        "color": "#3b82f6",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("model", [ServiceItem, ServiceCreate, ServiceUpdate])
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//example.com/without-scheme",
    ],
)
def test_service_models_reject_non_http_protocols(model: type, url: str) -> None:
    payload = {"url": url}
    if model is not ServiceUpdate:
        payload["name"] = "Unsafe service"
    if model is ServiceItem:
        payload["id"] = "unsafe-service"

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("model", [ServiceItem, ServiceCreate, ServiceUpdate])
@pytest.mark.parametrize("color", ["red", "#fff", "#12345g", "#000000;display:block"])
def test_service_models_reject_invalid_css_colors(model: type, color: str) -> None:
    payload = {"color": color}
    if model is not ServiceUpdate:
        payload.update(name="Unsafe service", url="https://example.com")
    if model is ServiceItem:
        payload["id"] = "unsafe-service"

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize(
    "service_id",
    ["", "UPPERCASE", "has space", "quote'break", 'quote"break', "../escape", "x<script>"],
)
def test_service_item_rejects_invalid_ids(service_id: str) -> None:
    with pytest.raises(ValidationError):
        ServiceItem(**_service_payload(id=service_id))


def test_service_models_accept_valid_browser_values() -> None:
    item = ServiceItem(**_service_payload(id="local-tool_2", url="http://127.0.0.1:8899"))
    created = ServiceCreate(name="Cloud", url="https://example.com/path?q=1", color="#A0b1C2")
    updated = ServiceUpdate(url="http://localhost:11434", color="#000000")

    assert item.id == "local-tool_2"
    assert created.color == "#A0b1C2"
    assert updated.url == "http://localhost:11434"


def test_catalog_active_text_is_rendered_inert_and_unsafe_urls_do_not_launch() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required for the portable DarkHub browser-boundary test")

    payload = _service_payload(
        name='<img src=x onerror="globalThis.__xss=1">',
        url="javascript:globalThis.__xss=2",
        description="</div><script>globalThis.__xss=3</script>",
        tags=["<svg onload=globalThis.__xss=4>"],
        color="#000000; background:url(javascript:globalThis.__xss=5)",
        pinned=True,
    )
    script = f"""
const fs = require("fs");
const vm = require("vm");
const elements = {{
  "services-grid": {{ innerHTML: "", addEventListener() {{}} }},
  "empty-state": {{ classList: {{ add() {{}}, remove() {{}} }} }},
  "quick-dock-items": {{ innerHTML: "" }},
  "quick-dock-section": {{ classList: {{ add() {{}}, remove() {{}} }} }}
}};
const opened = [];
const context = {{
  console,
  opened,
  setTimeout,
  clearTimeout,
  URL,
  catalogPayload: {json.dumps(payload)},
  fetch: async () => ({{ ok: false }}),
  navigator: {{ clipboard: {{ writeText: async () => {{}} }} }},
  document: {{
    activeElement: {{ tagName: "BODY" }},
    addEventListener() {{}},
    getElementById(id) {{ return elements[id] || null; }},
    createElement() {{ return {{ className: "", innerHTML: "", style: {{}}, remove() {{}} }}; }}
  }},
  window: {{ addEventListener() {{}}, open(url) {{ opened.push(url); }} }}
}};
vm.createContext(context);
const source = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8");
vm.runInContext(source + `
  state.services = [globalThis.catalogPayload];
  state.paletteResults = state.services;
  renderQuickDock();
  renderServices();
  launchPaletteItem(0);
  globalThis.result = {{
    grid: document.getElementById("services-grid").innerHTML,
    dock: document.getElementById("quick-dock-items").innerHTML,
    opened,
    executed: globalThis.__xss || 0
  }};
`, context);
process.stdout.write(JSON.stringify(context.result));
"""
    result = subprocess.run(
        [node, "-e", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    markup = rendered["grid"] + rendered["dock"]

    assert rendered["executed"] == 0
    assert rendered["opened"] == []
    assert "<script>" not in markup
    assert "<img" not in markup
    assert "<svg onload" not in markup
    assert "javascript:" not in markup.lower()
    assert "onclick=" not in markup.lower()
    assert "&lt;img" in markup
    assert "&lt;script&gt;" in markup
