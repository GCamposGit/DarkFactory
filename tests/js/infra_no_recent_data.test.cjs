const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "hub", "frontend", "infra.js"), "utf8");

const collapseSource = fs.readFileSync(path.join(__dirname, "..", "..", "hub", "frontend", "collapse.js"), "utf8");

function render(data) {
  const panel = { innerHTML: "" };
  const el = () => ({ addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, set innerHTML(v) {}, set textContent(v) {} });
  const ctx = {
    document: { getElementById: (id) => (id === "infra-metrics-panel" ? panel : el()), addEventListener() {}, querySelectorAll: () => [], hidden: false, readyState: "loading" },
    window: {}, console, fetch: async () => ({ ok: false }), setInterval() {}, setTimeout() {},
  };
  vm.createContext(ctx);
  vm.runInContext(collapseSource + ";\n" + source + "\n;this.__render = renderInfraMetrics;", ctx);
  ctx.__render(data);
  return panel.innerHTML;
}

test("no_recent_data node shows label and age, no hardware numbers", () => {
  const ago = new Date(Date.now() - 12 * 60000).toISOString();
  const html = render({
    host_hardware: { node_id: "darkfac-vps-primary" },
    containers: { items: [] },
    nodes: [
      { node_id: "predator-neo-16", role: "dev_workstation", status: "no_recent_data", is_live: false, hardware: null, last_contact_at: ago },
    ],
  });
  assert.match(html, /Sem dado recente/);
  assert.match(html, /há 12 min/);
});

test("missing notebook node does not fall back to predator-neo-16", () => {
  const html = render({ host_hardware: {}, containers: { items: [] }, nodes: [] });
  assert.ok(!html.includes("predator-neo-16"));
  assert.match(html, /nó desconhecido/);
});
