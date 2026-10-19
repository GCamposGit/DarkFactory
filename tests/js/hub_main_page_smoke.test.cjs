// USR-219 smoke: a pagina principal do Hub carrega sem as secoes removidas e sem lancar erros,
// com Conteineres/Catalogo recolhidos por padrao e o Estudio acessivel pelo menu lateral.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.join(__dirname, "..", "..", "hub", "frontend");
const html = fs.readFileSync(path.join(FRONTEND, "index.html"), "utf8");
const read = (name) => fs.readFileSync(path.join(FRONTEND, name), "utf8");

const SCRIPT_ORDER = [...html.matchAll(/<script src="\/static\/([\w.]+)\?v=\w+"><\/script>/g)].map((m) => m[1]);

function makeDom({ storage, readyState = "complete" } = {}) {
  const registry = new Map();
  function makeEl(id, parent) {
    let _id = id;
    const el = {
      parentNode: parent || null,
      children: [],
      style: {},
      dataset: {},
      attrs: {},
      _class: "",
      _html: "",
      listeners: new Map(),
      tagName: "DIV",
      textContent: "",
      get id() { return _id; },
      set id(v) { _id = v; if (v) registry.set(v, el); },
      get className() { return this._class; },
      set className(v) { this._class = String(v); },
      get classList() {
        const self = this;
        return {
          add: (c) => { if (!self._class.split(/\s+/).includes(c)) self._class = (self._class + " " + c).trim(); },
          remove: (c) => { self._class = self._class.split(/\s+/).filter((x) => x && x !== c).join(" "); },
          contains: (c) => self._class.split(/\s+/).includes(c),
          toggle(c, force) {
            const want = force === undefined ? !this.contains(c) : force;
            if (want) this.add(c); else this.remove(c);
            return want;
          },
        };
      },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = String(v); register(v, this); },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      addEventListener(name, cb) { this.listeners.set(name, cb); },
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      insertBefore(child) { child.parentNode = this; this.children.push(child); return child; },
      insertAdjacentElement() {},
      querySelector() { return null; },
      querySelectorAll() { return []; },
      remove() {},
      focus() {},
    };
    return el;
  }
  function register(markup, owner) {
    for (const m of String(markup).matchAll(/\sid="([^"$]+)"/g)) {
      if (!registry.has(m[1])) registry.set(m[1], makeEl(m[1], owner));
    }
  }
  register(html, null);

  const store = new Map(Object.entries(storage || {}));
  const localStorage = storage === null
    ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }
    : { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  const mainEl = makeEl("", makeEl("", null));
  const document = {
    hidden: false,
    readyState,
    body: makeEl("", null),
    getElementById: (id) => registry.get(id) || null,
    createElement: (tag) => { const el = makeEl("", null); el.tagName = tag.toUpperCase(); return el; },
    addEventListener() {},
    querySelector: (sel) => (sel === "main" ? mainEl : null),
    querySelectorAll: () => [],
  };
  const window = { localStorage, setTimeout: () => 0, clearTimeout() {}, addEventListener() {}, location: { search: "", hash: "" } };
  const ctx = {
    document, window, localStorage,
    console: { log() {}, warn() {}, error() {}, info() {} },
    fetch: async () => ({ ok: false, status: 503, json: async () => ({}) }),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    Date, JSON, Math, Promise, Headers: class { set() {} }, URLSearchParams, navigator: {},
  };
  window.document = document;
  vm.createContext(ctx);
  return { ctx, registry, store, document };
}

test("index.html nao traz mais a fila de tarefas, os cartoes WAITING_HUMAN nem o Estudio na pagina", () => {
  for (const id of ["tasks-dashboard-summary", "tasks-dashboard-queue", "tasks-queue-container", "tasks-view-btn-queue", "tasks-view-btn-dag", "tasks-dashboard-refresh", "tasks-dashboard-alert"]) {
    assert.ok(!html.includes(`id="${id}"`), `${id} deveria ter saido`);
  }
  assert.ok(!html.includes("Aguardando Decisão do Owner"));
  assert.ok(!html.includes("Fila operacional & Autonomia"));
  const mainBody = html.slice(html.indexOf("<main"), html.indexOf("</main>"));
  assert.ok(!mainBody.includes("content-studio-section"));
  assert.ok(!mainBody.includes("studio-drawer"));
  assert.match(html, /id="studio-drawer-body"/);
  assert.match(html, /onclick="openStudioDrawer\(\)"\s+title="Estúdio de conteúdo e ateliê visual"/);
  assert.ok(html.includes('id="tasks-dashboard-section"'), "ancora usada por health.js e hub/coverage.json");
  // todo corpo recolhivel referenciado por um botao existe
  for (const m of html.matchAll(/data-collapse-target="([\w-]+)"/g)) {
    assert.ok(html.includes(`id="${m[1]}"`), `alvo ${m[1]} ausente`);
  }
});

test("carrega os scripts sem lancar e monta secoes recolhidas por padrao", () => {
  const dom = makeDom();
  assert.ok(SCRIPT_ORDER.indexOf("collapse.js") > -1);
  assert.ok(SCRIPT_ORDER.indexOf("collapse.js") < SCRIPT_ORDER.indexOf("catalog.js"));
  assert.ok(SCRIPT_ORDER.indexOf("collapse.js") < SCRIPT_ORDER.indexOf("infra.js"));
  const failures = [];
  for (const name of ["collapse.js", "tasks.js", "catalog.js", "studio.js"]) {
    try {
      vm.runInContext(read(name), dom.ctx, { filename: name });
    } catch (err) {
      failures.push(`${name}: ${err.message}`);
    }
  }
  assert.deepEqual(failures, []);

  const studio = dom.registry.get("content-studio-section");
  assert.ok(studio, "Estudio deve montar");
  assert.equal(studio.parentNode.id, "studio-drawer-body");

  const catalog = dom.registry.get("cross-catalog-section");
  assert.ok(catalog);
  assert.match(catalog.innerHTML, /id="cross-catalog-body" class="space-y-4 hidden"/);
  assert.match(catalog.innerHTML, /aria-expanded="false"[^>]*>Abrir</);
});

test("estado aberto lembrado por navegador e storage bloqueado nao quebra", () => {
  const open = makeDom({ storage: { "darkhub.collapse.cross-catalog": "open" } });
  for (const name of ["collapse.js", "catalog.js"]) vm.runInContext(read(name), open.ctx);
  assert.doesNotMatch(open.registry.get("cross-catalog-section").innerHTML, /cross-catalog-body" class="space-y-4 hidden"/);

  const blocked = makeDom({ storage: null });
  for (const name of ["collapse.js", "catalog.js"]) vm.runInContext(read(name), blocked.ctx);
  assert.match(blocked.registry.get("cross-catalog-section").innerHTML, /cross-catalog-body" class="space-y-4 hidden"/);

  const dom = makeDom();
  vm.runInContext(read("collapse.js"), dom.ctx);
  const body = dom.document.createElement("div");
  body.id = "any-body";
  body.className = "hidden";
  const button = dom.document.createElement("button");
  button.attrs = { "data-collapse-key": "demo", "data-collapse-target": "any-body" };
  const toggle = vm.runInContext("hubToggleCollapse", dom.ctx);
  toggle(button);
  assert.equal(dom.store.get("darkhub.collapse.demo"), "open");
  assert.ok(!body.classList.contains("hidden"));
  toggle(button);
  assert.equal(dom.store.get("darkhub.collapse.demo"), "closed");
  assert.equal(button.textContent, "Abrir");
});

test("conteineres e cargas ativas renderizam recolhidos", () => {
  const dom = makeDom({ readyState: "loading" });
  vm.runInContext(read("collapse.js"), dom.ctx);
  vm.runInContext(read("infra.js") + "\n;this.__render = renderInfraMetrics;", dom.ctx);
  dom.document.createElement("div").id = "infra-metrics-panel";
  dom.ctx.__render({ host_hardware: {}, containers: { items: [] }, nodes: [] });
  const panel = dom.document.getElementById("infra-metrics-panel");
  assert.ok(panel, "painel de metricas existe");
  assert.match(panel.innerHTML, /id="infra-containers-body" class="hidden overflow-x-auto/);
  assert.match(panel.innerHTML, /data-collapse-key="infra-containers"/);
});
