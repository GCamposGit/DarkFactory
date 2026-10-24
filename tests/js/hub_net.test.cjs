const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "hub", "frontend", "hub_net.js"), "utf8");

function load(responses) {
  const calls = [];
  const banner = { hidden: true, style: {}, setAttribute() {}, id: "" };
  const document = {
    body: { appendChild() {} },
    getElementById: () => (banner.created ? banner : null),
    createElement: () => { banner.created = true; return banner; },
  };
  const queue = [...responses];
  const ctx = {
    document, window: {}, console,
    setTimeout: (fn) => { fn(); return 0; }, // instant backoff
    fetch: async (url, options) => {
      calls.push([url, (options && options.method) || "GET"]);
      const next = queue.length > 1 ? queue.shift() : queue[0];
      if (next instanceof Error) throw next;
      return next;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source + "\n;this.__fetch = hubNetFetch;", ctx);
  return { ctx, calls, banner };
}

test("GET repete apos falha de rede e esconde o banner ao recuperar", async () => {
  const ok = { ok: true, status: 200 };
  const { ctx, calls, banner } = load([new TypeError("Failed to fetch"), new TypeError("Failed to fetch"), ok]);
  const res = await ctx.__fetch("/api/x");
  assert.equal(res, ok);
  assert.equal(calls.length, 3);
  assert.equal(banner.hidden, true);
  assert.match(banner.textContent, /Hub atualizando, reconectando/);
});

test("GET do snapshot repete apos 503 e preserva o AbortController", async () => {
  const ok = { ok: true, status: 200 };
  const retry = load([{ ok: false, status: 503 }, ok]);
  const controller = new AbortController();
  assert.equal(await retry.ctx.__fetch("/api/line/snapshot", { signal: controller.signal }), ok);
  assert.equal(retry.calls.length, 2);
  assert.deepEqual(retry.calls.map(([url]) => url), ["/api/line/snapshot", "/api/line/snapshot"]);

  const aborted = new Error("aborted");
  aborted.name = "AbortError";
  const noRetry = load([aborted, ok]);
  await assert.rejects(() => noRetry.ctx.__fetch("/api/line/snapshot", { signal: controller.signal }), (error) => error.name === "AbortError");
  assert.equal(noRetry.calls.length, 1);
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
