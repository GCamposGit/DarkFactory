const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "hub", "frontend", "tasks.js"),
  "utf8"
);

function deferred() {
  let resolve;
  const promise = new Promise((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

// USR-219: tasks.js nao renderiza mais a fila; mantem so o status da linha (USR-215) e o DAG.
function setup(fetchImpl) {
  const listeners = new Map();
  const elements = new Map();
  const timers = new Map();
  let nextTimer = 1;
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        textContent: "",
        innerHTML: "",
        className: "",
        classList: { toggle() {}, add() {}, remove() {} },
        setAttribute() {},
        addEventListener(name, callback) { this.listeners.set(name, callback); },
        listeners: new Map(),
      });
    }
    return elements.get(id);
  };
  const document = {
    hidden: false,
    addEventListener(name, callback) { listeners.set(name, callback); },
    getElementById(id) { return element(id); },
  };
  const window = {
    setTimeout(callback, delay) {
      assert.equal(delay, 30000);
      const id = nextTimer++;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({ document, window, fetch: fetchImpl, console, Date });
  vm.runInContext(source, context);
  const state = vm.runInContext("taskDashboardState", context);
  const flush = () => new Promise(setImmediate);
  return { document, element, listeners, timers, state, flush, context };
}

test("a fila de tarefas e os cartoes WAITING_HUMAN nao existem mais no tasks.js", () => {
  assert.ok(!/api\/tasks\/dashboard/.test(source));
  assert.ok(!/renderWaitingHumanObservationCard|renderTaskDashboard|loadTaskDashboard/.test(source));
  assert.ok(!source.includes("Aguardando Decisão do Owner"));
  assert.equal(typeof vm.runInContext("loadLineStatus", setup(async () => ({})).context), "function");
  assert.equal(typeof vm.runInContext("renderLineCriterionHint", setup(async () => ({})).context), "function");
});

test("polls line status only while visible, coalescing overlapping requests", async () => {
  const first = deferred();
  let lineRequests = 0;
  let active = 0;
  let maxActive = 0;
  const urls = [];
  const ui = setup(async (url) => {
    urls.push(url);
    lineRequests++;
    active++;
    maxActive = Math.max(active, maxActive);
    const response = lineRequests === 1
      ? await first.promise
      : { ok: true, json: async () => ({ canary_streak: 2 }) };
    active--;
    return response;
  });
  ui.listeners.get("DOMContentLoaded")();
  assert.equal(lineRequests, 1);
  assert.equal(ui.timers.size, 1);
  ui.document.hidden = true;
  ui.listeners.get("visibilitychange")();
  assert.equal(ui.timers.size, 0);
  ui.document.hidden = false;
  ui.listeners.get("visibilitychange")();
  assert.equal(lineRequests, 1);
  first.resolve({ ok: true, json: async () => ({ canary_streak: 1 }) });
  await ui.flush();
  assert.equal(lineRequests, 2);
  assert.equal(maxActive, 1);
  assert.deepEqual([...new Set(urls)], ["/api/line/status"]);
  assert.equal(ui.element("line-canary-streak-badge").textContent, "Canário: 2 dias verdes");
  assert.equal(ui.timers.size, 1);
});
