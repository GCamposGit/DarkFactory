const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "hub", "frontend", "hub_net.js"), "utf8");

function load(responses) {
  const calls = [];
  const delays = [];
  const banner = { hidden: true, style: {}, setAttribute() {}, id: "" };
  const document = {
    body: { appendChild() {} },
    getElementById: () => (banner.created ? banner : null),
    createElement: () => { banner.created = true; return banner; },
  };
  const queue = [...responses];
  const ctx = {
    document, window: {}, console,
    setTimeout: (fn, ms) => { delays.push(ms); queueMicrotask(fn); return 0; }, // instant backoff/watcher
    fetch: async (url, options) => {
      calls.push([url, (options && options.method) || "GET"]);
      if (url === "/health") return { ok: true, status: 200 };
      const next = queue.length > 1 ? queue.shift() : queue[0];
      if (next instanceof Error) throw next;
      return next;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source + "\n;this.__fetch = hubNetFetch;", ctx);
  return { ctx, calls, banner, delays };
}

test("GET repete apos falha de rede e esconde o banner ao recuperar", async () => {
  const ok = { ok: true, status: 200 };
  const { ctx, calls, banner, delays } = load([new TypeError("Failed to fetch"), new TypeError("Failed to fetch"), ok]);
  const res = await ctx.__fetch("/api/x");
  assert.equal(res, ok);
  assert.equal(calls.length, 3);
  assert.deepEqual(delays, [500, 1000]);
  assert.equal(banner.hidden, true);
  assert.match(banner.textContent, /Hub atualizando, reconectando/);
});

test("hubNetMaxAttempts=1 nao repete, mostra banner e inicia o vigia", async () => {
  const { ctx, calls, banner } = load([new TypeError("Failed to fetch")]);
  await assert.rejects(() => ctx.__fetch("/api/line/live", { hubNetMaxAttempts: 1 }), /Hub atualizando/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.filter(([url]) => url === "/api/line/live").length, 1);
  assert.ok(calls.some(([url]) => url === "/health"));
  assert.match(banner.textContent, /Hub atualizando, reconectando/);
});

test("AbortError e propagado sem ser convertido", async () => {
  const abort = new Error("aborted");
  abort.name = "AbortError";
  const { ctx, calls } = load([abort]);
  await assert.rejects(() => ctx.__fetch("/api/line/live", { hubNetMaxAttempts: 1 }), (error) => error === abort);
  assert.equal(calls.length, 1);
});

test("503 do proxy e repetido; POST com erro de rede nao e reenviado", async () => {
  const ok = { ok: true, status: 200 };
  const a = load([{ ok: false, status: 503 }, ok]);
  assert.equal(await a.ctx.__fetch("/api/x"), ok);
  assert.equal(a.calls.length, 2);
  const b = load([new TypeError("Failed to fetch")]);
  b.ctx.setTimeout = () => 0; // do not let the watcher loop spin
  await assert.rejects(() => b.ctx.__fetch("/api/x", { method: "POST" }), /Hub atualizando/);
  assert.equal(b.calls.length, 1);
});
