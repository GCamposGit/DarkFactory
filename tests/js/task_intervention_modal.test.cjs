const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "hub", "frontend", "interventions.js"),
  "utf8"
);

function load(fetchImpl) {
  const content = { innerHTML: "" };
  const classes = new Set(["hidden"]);
  const modal = { classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) } };
  const feedback = { textContent: "" };
  const elements = {
    "task-intervention-modal": modal,
    "task-intervention-modal-content": content,
    "task-intervention-modal-title": { textContent: "" },
    "task-intervention-modal-subtitle": { textContent: "" },
    "task-intervention-feedback": feedback,
  };
  const scrolled = [];
  const context = {
    console,
    setTimeout: () => 0,
    history: {},
    confirm: () => true,
    hubFetch: fetchImpl,
    window: { location: { hash: "" } },
    document: {
      addEventListener() {},
      hidden: false,
      body: { classList: { add() {}, remove() {} } },
      getElementById: (id) => {
        const el = elements[id];
        return el ? Object.assign(el, { scrollIntoView: () => scrolled.push(id) }) : null;
      },
    },
    module: { exports: {} },
  };
  context.window.window = context.window;
  context.setInterval = () => 0;
  context.loadPriorityInterventions = () => {};
  vm.createContext(context);
  vm.runInContext(source + "\n;globalThis.__api = { interventionsState, openTaskInterventionModal, runTaskInterventionAction };", context);
  return { api: context.__api, exported: context.module.exports, content, classes, feedback, scrolled };
}

const item = (cause, meta = {}) => ({
  id: "waiting_human:dem-722f8e52bd7c",
  kind: "waiting_human",
  title: "Bloqueio WAITING_HUMAN: dem-722f8e52bd7c - Canario",
  description: `Causa: ${cause}`,
  metadata: { task_id: "dem-722f8e52bd7c", ticket_id: "dem-722f8e52bd7c", run_id: "run-d4d10d4aee94", cause_code: cause, ...meta },
});

test("todo cause_code conhecido tem instrucao e acoes", () => {
  const { exported } = load(async () => ({ ok: true, json: async () => ({}) }));
  for (const code of Object.keys(exported.TASK_CAUSE_GUIDES)) {
    const guide = exported.resolveTaskInterventionGuide(code);
    assert.ok(guide.known && guide.instructions.length > 20, code);
  }
  assert.ok(exported.resolveTaskInterventionGuide("waiting_human_integration").known);
});

test("grill_pending oferece Responder Grill e Cancelar", () => {
  const { exported } = load(async () => ({}));
  assert.equal(JSON.stringify(exported.taskInterventionActions(item("grill_pending")).map((a) => a.kind)), JSON.stringify(["grill", "cancel"]));
});

test("cause_code desconhecido mostra evidencia crua e so cancelar", () => {
  const { api, exported, content, classes } = load(async () => ({}));
  const it = item("causa_inedita_xyz", { evidence: [{ label: "projeto", value: "darkfac" }] });
  api.interventionsState.report = { items: [it] };
  assert.equal(JSON.stringify(exported.taskInterventionActions(it).map((a) => a.kind)), JSON.stringify(["cancel"]));
  api.openTaskInterventionModal(it.id);
  assert.ok(!classes.has("hidden"));
  assert.match(content.innerHTML, /Evidência crua/);
  assert.match(content.innerHTML, /causa_inedita_xyz/);
  assert.match(content.innerHTML, /projeto: darkfac/);
});

test("Ver Tarefa nunca rola a pagina e cancelar usa o endpoint do run", async () => {
  const calls = [];
  const { api, feedback } = load(async (url, opts) => {
    calls.push([url, opts.method]);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  const it = item("grill_pending");
  api.interventionsState.report = { items: [it] };
  api.openTaskInterventionModal(it.id);
  await api.runTaskInterventionAction("cancel");
  const posts = calls.filter((c) => c[1] === "POST");
  assert.equal(JSON.stringify(posts), JSON.stringify([["/api/line/runs/run-d4d10d4aee94/cancel", "POST"]]));
  const modalCode = source.slice(source.indexOf("function openTaskInterventionModal"));
  assert.ok(!modalCode.includes("scrollIntoView"));
  assert.equal(feedback.textContent, "Run cancelado.");
  assert.ok(!/tasks-dashboard-section[^]*?<span>Ver Tarefa/.test(source));
});

test("sem ids nenhum botao aparece", () => {
  const { exported } = load(async () => ({}));
  assert.equal(exported.taskInterventionActions({ metadata: { cause_code: "grill_pending" } }).length, 0);
});
