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
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function dashboardResponse(updatedAt = new Date(Date.now() - 5 * 60_000).toISOString()) {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      sources: { control: "sqlite:ok" },
      warnings: [],
      queue: [{ task_id: "USR-128", title: "Job visível", status: "RUNNING", updated_at: updatedAt }],
    }),
  };
}

function setup(fetchImpl, { lineStatus = false } = {}) {
  const listeners = new Map();
  const elements = new Map();
  const timers = new Map();
  let nextTimer = 1;
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        textContent: "",
        innerHTML: "",
        className: "",
        listeners: new Map(),
        addEventListener(name, callback) { this.listeners.set(name, callback); },
      });
    }
    return elements.get(id);
  };
  const document = {
    hidden: false,
    addEventListener(name, callback) { listeners.set(name, callback); },
    getElementById(id) { return id === "line-status-card" && !lineStatus ? null : element(id); },
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
  return { document, element, listeners, timers, state, flush };
}

test("polls only while visible and refreshes on return without overlapping requests", async () => {
  const first = deferred();
  let requests = 0;
  let active = 0;
  let maxActive = 0;
  const ui = setup(async () => {
    requests++;
    active++;
    maxActive = Math.max(active, maxActive);
    const response = requests === 1 ? await first.promise : dashboardResponse();
    active--;
    return response;
  });

  ui.listeners.get("DOMContentLoaded")();
  assert.equal(requests, 1);
  assert.equal(ui.timers.size, 1);
  ui.document.hidden = true;
  ui.listeners.get("visibilitychange")();
  assert.equal(ui.timers.size, 0);
  ui.document.hidden = false;
  ui.listeners.get("visibilitychange")();
  assert.equal(requests, 1);
  assert.equal(ui.state.refreshPending, true);
  first.resolve(dashboardResponse());
  await ui.flush();
  assert.equal(requests, 2);
  assert.equal(maxActive, 1);
  assert.equal(ui.timers.size, 1);
  assert.match(ui.element("tasks-dashboard-queue").innerHTML, /Última atividade: há 5 min/);

  const timer = [...ui.timers.values()][0];
  ui.timers.clear();
  timer();
  await ui.flush();
  assert.equal(requests, 3);
  assert.equal(ui.timers.size, 1);
});

test("source failure keeps the prior queue and successful refresh time visible", async () => {
  let requests = 0;
  const ui = setup(async () => {
    requests++;
    if (requests === 1) return dashboardResponse();
    return { ok: true, status: 200, json: async () => ({ sources: { control: "sqlite:missing" }, queue: [] }) };
  });
  ui.listeners.get("DOMContentLoaded")();
  await ui.flush();
  const lastSuccessAt = ui.state.lastSuccessAt;
  const timer = [...ui.timers.values()][0];
  ui.timers.clear();
  timer();
  await ui.flush();
  assert.equal(requests, 2);
  assert.equal(ui.state.lastSuccessAt, lastSuccessAt);
  assert.match(ui.element("tasks-dashboard-alert").textContent, /Fila indisponível: fonte de controle sqlite:missing/);
  assert.match(ui.element("tasks-dashboard-alert").textContent, /Última atualização bem-sucedida às/);
  assert.match(ui.element("tasks-dashboard-queue").innerHTML, /Job visível/);
});

test("line status also coalesces a visible refresh while one request is pending", async () => {
  const firstLine = deferred();
  let lineRequests = 0;
  let lineActive = 0;
  let lineMaxActive = 0;
  const ui = setup(async (url) => {
    if (url === "/api/tasks/dashboard") return dashboardResponse();
    lineRequests++;
    lineActive++;
    lineMaxActive = Math.max(lineActive, lineMaxActive);
    const response = lineRequests === 1
      ? await firstLine.promise
      : { ok: true, json: async () => ({ canary_streak: 2 }) };
    lineActive--;
    return response;
  }, { lineStatus: true });
  ui.listeners.get("DOMContentLoaded")();
  ui.document.hidden = true;
  ui.listeners.get("visibilitychange")();
  ui.document.hidden = false;
  ui.listeners.get("visibilitychange")();
  assert.equal(lineRequests, 1);
  firstLine.resolve({ ok: true, json: async () => ({ canary_streak: 1 }) });
  await ui.flush();
  assert.equal(lineRequests, 2);
  assert.equal(lineMaxActive, 1);
  assert.equal(ui.element("line-canary-streak-badge").textContent, "Canário: 2 dias verdes");
});
