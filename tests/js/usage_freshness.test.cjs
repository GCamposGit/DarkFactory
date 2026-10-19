const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "hub", "frontend", "usage.js"), "utf8");

function load() {
  const el = () => ({ addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, set innerHTML(v) {}, set textContent(v) {} });
  const ctx = {
    document: {
      getElementById: () => el(), addEventListener() {}, querySelectorAll: () => [], readyState: "loading", hidden: false,
      createElement: () => {
        let text = "";
        return { set textContent(v) { text = v; }, get innerHTML() { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); } };
      },
    },
    window: {}, console, fetch: async () => ({ ok: false }), setInterval() {}, setTimeout() {}, localStorage: { getItem: () => null, setItem() {} },
  };
  vm.createContext(ctx);
  vm.runInContext(source + "\n;this.__card = renderAccountCard; this.__fresh = usageFreshness;", ctx);
  return ctx;
}

const base = { provider_id: "anthropic", provider_name: "Anthropic", family: "frontier", status: "connected", message: "ok",
  windows: [{ quota_id: "w", label: "5h", used_percent: 40, remaining_percent: 60, window_duration_minutes: 300 }] };

test("recent snapshot shows age and origin without stale badge", () => {
  const { __card: card } = load();
  const html = card({ ...base, age_seconds: 120, stale: false, source_node: "predator-neo-16", checked_at: "2030-01-01T11:58:00Z" });
  assert.match(html, /há 2 min · origem: predator-neo-16/);
  assert.doesNotMatch(html, /data-stale-badge/);
});

test("stale snapshot shows highlighted badge and keeps the percentage", () => {
  const { __card: card } = load();
  const html = card({ ...base, age_seconds: 601, stale: true, source_node: "n1", checked_at: "2030-01-01T11:49:00Z" });
  assert.match(html, /data-stale-badge/);
  assert.match(html, /desatualizado/);
  assert.match(html, /40/);
  assert.doesNotMatch(html, /desconectado/i);
});

test("no synced data is neither stale nor shown as zero", () => {
  const { __card: card, __fresh: fresh } = load();
  const account = { ...base, status: "unknown", windows: [], message: "Sem dado sincronizado", age_seconds: null, stale: false };
  assert.deepEqual({ ...fresh(account) }, { stale: false, text: "sem dado sincronizado" });
  const html = card(account);
  assert.doesNotMatch(html, /data-stale-badge/);
  assert.match(html, /sem dado sincronizado/);
});
