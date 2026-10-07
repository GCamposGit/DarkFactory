"""USR-61: Melhoria nas acoes do usuario no Dark Hub (reconhecer alertas).

Covers the owner's alert strip (`#factory-alerts-strip`):

1. Acknowledging ONE alert needs no confirmation box and shows a success
   message that disappears on its own after 3 seconds.
2. Several alerts can be selected and acknowledged with a single click; the
   state stays correct (selection cleared on success, failed ones stay listed
   and selected, acknowledged ones leave the list).
3. The backend contract the frontend relies on (existing per-item endpoint,
   unknown id answers 200 with acknowledged=false, session required).

The frontend logic is exercised for real with Node (health.js + health_ops.js
loaded in one vm context against a tiny fake DOM) when `node` is available;
static DOM-contract tests always run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.notifications.models import AlertCategory, AlertSeverity, NotificationEvent
from core.notifications.store import NotificationStore
from hub.backend.api import get_hub_service
from hub.backend.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"
HEALTH_JS = FRONTEND_DIR / "health.js"
HEALTH_OPS_JS = FRONTEND_DIR / "health_ops.js"

_HARNESS_JS = r"""
const fs = require("fs");
const vm = require("vm");

const [healthPath, opsPath] = process.argv.slice(2);

class FakeClassList {
  constructor(initial) { this.set = new Set(initial); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
}
class FakeEl {
  constructor(hidden) {
    this.classList = new FakeClassList(hidden ? ["hidden"] : []);
    this.textContent = "";
    this.innerHTML = "";
    this.disabled = false;
    this.checked = false;
    this.indeterminate = false;
    this.attrs = {};
  }
  set className(value) { this.classList = new FakeClassList(String(value).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classList.set].join(" "); }
  setAttribute(k, v) { this.attrs[k] = v; }
}

const els = {
  "factory-alerts-strip": new FakeEl(true),
  "factory-alerts-toolbar": new FakeEl(true),
  "factory-alerts-list": new FakeEl(false),
  "factory-alerts-status": new FakeEl(true),
  "factory-alerts-selected-count": new FakeEl(false),
  "factory-alerts-ack-selected": new FakeEl(false),
  "factory-alerts-select-all": new FakeEl(false),
};

const timers = [];
const calls = { fetch: [], confirm: 0, alert: 0 };
let serverUnread = [];
let failIds = new Set();
let missingIds = new Set();

const fakeFetch = async (url, options = {}) => {
  const method = (options.method || "GET").toUpperCase();
  const m = /\/api\/notifications\/([^/?]+)\/acknowledge/.exec(url);
  calls.fetch.push({ url, method });
  if (m && method === "POST") {
    const id = decodeURIComponent(m[1]);
    if (failIds.has(id)) return { ok: false, status: 500, json: async () => ({}) };
    if (missingIds.has(id)) return { ok: true, status: 200, json: async () => ({ notification_id: id, acknowledged: false }) };
    serverUnread = serverUnread.filter((n) => n.notification_id !== id);
    return { ok: true, status: 200, json: async () => ({ notification_id: id, acknowledged: true }) };
  }
  if (/\/api\/notifications\?/.test(url)) {
    return { ok: true, status: 200, json: async () => ({ notifications: serverUnread, count: serverUnread.length }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

const context = {
  console,
  AbortController,
  Headers,
  Promise,
  Set,
  Date,
  encodeURIComponent,
  decodeURIComponent,
  fetch: fakeFetch,
  confirm: () => { calls.confirm += 1; return true; },
  alert: () => { calls.alert += 1; },
  setTimeout: (fn, ms) => { timers.push({ fn, ms, cleared: false }); return timers.length - 1; },
  clearTimeout: (id) => { if (timers[id]) timers[id].cleared = true; },
  setInterval: () => 0,
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    readyState: "loading",
    visibilityState: "visible",
    addEventListener: () => {},
    getElementById: (id) => els[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => new FakeEl(false),
    body: { appendChild: () => {} },
  },
};
context.window = context;
context.window.sessionToken = "test-token";
vm.createContext(context);
vm.runInContext(fs.readFileSync(healthPath, "utf8"), context);
vm.runInContext(fs.readFileSync(opsPath, "utf8"), context);

const notif = (id, severity = "warning") => ({
  notification_id: id, title: `Alerta ${id}`, message: `Mensagem ${id}`, severity,
});

function run(code) { return vm.runInContext(code, context); }

function resetScenario(ids, { fail = [], missing = [] } = {}) {
  serverUnread = ids.map((id) => notif(id));
  failIds = new Set(fail);
  missingIds = new Set(missing);
  calls.fetch.length = 0;
  calls.confirm = 0;
  calls.alert = 0;
  timers.length = 0;
  run("factoryAlertsState.statusTimer = null; factoryAlertsState.selected = new Set(); factoryAlertsState.busy = new Set(); factoryAlertsState.batchBusy = false;");
  els["factory-alerts-status"].textContent = "";
  els["factory-alerts-status"].className = "hidden";
  context.renderFactoryAlerts(serverUnread);
}

function snapshot() {
  const status = els["factory-alerts-status"];
  const successTimers = timers.filter((t) => t.ms === 3000 && !t.cleared);
  return {
    itemIds: run("factoryAlertsState.items.map((n) => n.notification_id)"),
    selected: run("[...factoryAlertsState.selected].sort()"),
    statusText: status.textContent,
    statusHidden: status.classList.contains("hidden"),
    statusIsSuccess: status.className.includes("emerald"),
    stripHidden: els["factory-alerts-strip"].classList.contains("hidden"),
    ackBtnDisabled: els["factory-alerts-ack-selected"].disabled,
    successTimerCount: successTimers.length,
    posts: calls.fetch.filter((c) => c.method === "POST").map((c) => decodeURIComponent(/notifications\/([^/]+)\//.exec(c.url)[1])),
    confirmCalls: calls.confirm,
    alertCalls: calls.alert,
  };
}

async function flush() { for (let i = 0; i < 20; i += 1) await Promise.resolve(); await new Promise((r) => setImmediate(r)); }

(async () => {
  const out = {};

  // Pure helpers.
  out.summary = {
    one: context.healthAckSummary(1, 0),
    many: context.healthAckSummary(3, 0),
    partial: context.healthAckSummary(2, 1),
    none: context.healthAckSummary(0, 2),
  };
  out.reconcile = [...context.healthReconcileSelection(new Set(["a", "b", "z"]), [notif("a"), notif("b"), notif("c")])].sort();

  // 1. Single acknowledge: no confirm/alert, success message, disappears after 3 s.
  resetScenario(["a", "b", "c"]);
  await context.acknowledgeNotification("b");
  await flush();
  out.single = snapshot();
  const t3 = timers.find((t) => t.ms === 3000 && !t.cleared);
  t3.fn();
  out.singleAfterTimer = snapshot();

  // 2. Batch, everything succeeds: selection cleared, list emptied, strip stays
  //    visible for the message, then hides after 3 s.
  resetScenario(["a", "b", "c"]);
  context.toggleFactoryAlertSelection("a", true);
  context.toggleFactoryAlertSelection("c", true);
  out.batchSelectedBefore = snapshot();
  await context.acknowledgeSelectedNotifications();
  await flush();
  out.batchOk = snapshot();

  resetScenario(["a", "b"]);
  context.toggleAllFactoryAlerts(true);
  await context.acknowledgeSelectedNotifications();
  await flush();
  out.batchAll = snapshot();
  timers.find((t) => t.ms === 3000 && !t.cleared).fn();
  out.batchAllAfterTimer = snapshot();

  // 3. Partial failure: failed (HTTP 500) and not-found (acknowledged=false)
  //    keep their place in the list and stay selected; ok ones leave.
  resetScenario(["a", "b", "c", "d"], { fail: ["c"], missing: ["d"] });
  context.toggleAllFactoryAlerts(true);
  await context.acknowledgeSelectedNotifications();
  await flush();
  out.batchPartial = snapshot();

  // 4. Re-entrancy: a second click while the batch runs does nothing.
  resetScenario(["a", "b"]);
  context.toggleAllFactoryAlerts(true);
  const first = context.acknowledgeSelectedNotifications();
  const second = context.acknowledgeSelectedNotifications();
  await Promise.all([first, second]);
  await flush();
  out.reentrant = snapshot();

  // 5. Empty selection is a no-op.
  resetScenario(["a"]);
  await context.acknowledgeSelectedNotifications();
  out.emptySelection = snapshot();

  // 6. A poll that returns fewer alerts prunes stale selection.
  resetScenario(["a", "b"]);
  context.toggleAllFactoryAlerts(true);
  context.renderFactoryAlerts([notif("b")]);
  out.pruned = snapshot();

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def node_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available; DOM-contract tests still cover the wiring")
    harness = tmp_path_factory.mktemp("usr61") / "harness.js"
    harness.write_text(_HARNESS_JS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(HEALTH_JS), str(HEALTH_OPS_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Behaviour (Node)
# ---------------------------------------------------------------------------


def test_summary_messages(node_run: dict) -> None:
    summary = node_run["summary"]
    assert summary["one"] == {"kind": "success", "text": "Alerta reconhecido."}
    assert summary["many"] == {"kind": "success", "text": "3 alertas reconhecidos."}
    assert summary["partial"]["kind"] == "error"
    assert "2 reconhecidos" in summary["partial"]["text"] and "1 falharam" in summary["partial"]["text"]
    assert summary["none"]["kind"] == "error"


def test_single_acknowledge_has_no_confirmation_and_message_vanishes_after_3s(node_run: dict) -> None:
    single = node_run["single"]
    assert single["confirmCalls"] == 0 and single["alertCalls"] == 0
    assert single["posts"] == ["b"]
    assert single["itemIds"] == ["a", "c"]
    assert single["statusText"] == "Alerta reconhecido."
    assert single["statusIsSuccess"] is True and single["statusHidden"] is False
    assert single["successTimerCount"] == 1

    after = node_run["singleAfterTimer"]
    assert after["statusText"] == ""
    assert after["statusHidden"] is True
    assert after["stripHidden"] is False  # two alerts remain


def test_batch_acknowledges_all_selected_and_clears_selection(node_run: dict) -> None:
    before = node_run["batchSelectedBefore"]
    assert before["selected"] == ["a", "c"]
    assert before["ackBtnDisabled"] is False

    ok = node_run["batchOk"]
    assert ok["posts"] == ["a", "c"]
    assert ok["itemIds"] == ["b"]
    assert ok["selected"] == []
    assert ok["ackBtnDisabled"] is True
    assert ok["statusText"] == "2 alertas reconhecidos."
    assert ok["confirmCalls"] == 0 and ok["alertCalls"] == 0


def test_batch_of_last_alerts_keeps_message_visible_then_hides_strip(node_run: dict) -> None:
    done = node_run["batchAll"]
    assert done["itemIds"] == []
    assert done["statusText"] == "2 alertas reconhecidos."
    assert done["stripHidden"] is False, "the success message must stay visible even with no alerts left"
    assert done["successTimerCount"] == 1

    later = node_run["batchAllAfterTimer"]
    assert later["statusText"] == ""
    assert later["stripHidden"] is True


def test_partial_failure_keeps_only_failed_selected_and_listed(node_run: dict) -> None:
    partial = node_run["batchPartial"]
    assert partial["posts"] == ["a", "b", "c", "d"], "one failure must not abort the remaining items"
    assert partial["itemIds"] == ["c", "d"]
    assert partial["selected"] == ["c", "d"]
    assert partial["statusIsSuccess"] is False
    assert "2 reconhecidos" in partial["statusText"] and "2 falharam" in partial["statusText"]
    assert partial["ackBtnDisabled"] is False


def test_second_click_during_batch_is_ignored(node_run: dict) -> None:
    assert node_run["reentrant"]["posts"] == ["a", "b"]


def test_empty_selection_does_nothing(node_run: dict) -> None:
    empty = node_run["emptySelection"]
    assert empty["posts"] == []
    assert empty["statusText"] == ""


def test_poll_prunes_selection_of_alerts_that_disappeared(node_run: dict) -> None:
    assert node_run["pruned"]["selected"] == ["b"]
    assert node_run["reconcile"] == ["a", "b"]


# ---------------------------------------------------------------------------
# DOM contract (static)
# ---------------------------------------------------------------------------


def _ack_section(text: str) -> str:
    start = text.index("// 1. Acknowledge Alert")
    end = text.index("// 2. Check Token Quotas")
    return text[start:end]


def test_acknowledge_path_has_no_confirmation_dialog() -> None:
    ops = HEALTH_OPS_JS.read_text(encoding="utf-8")
    section = _ack_section(ops)
    assert "function acknowledgeNotification(" in section
    assert "function acknowledgeSelectedNotifications(" in section
    assert "confirmAcknowledgeNotification" not in ops
    assert not re.search(r"\b(confirm|alert)\(", section), "no confirm()/alert() on the acknowledge path"
    assert "openHealthOpsModal" not in section and "health-ops-modal" not in section
    health = HEALTH_JS.read_text(encoding="utf-8")
    assert "confirmAcknowledgeNotification" not in health
    assert 'onclick="acknowledgeNotification(' in health


def test_success_message_timeout_is_three_seconds() -> None:
    health = HEALTH_JS.read_text(encoding="utf-8")
    assert re.search(r"FACTORY_ALERTS_SUCCESS_MS\s*=\s*3000\b", health)
    assert "setTimeout(" in health[health.index("function showFactoryAlertsStatus(") :]


def test_status_region_and_checkboxes_are_accessible() -> None:
    health = HEALTH_JS.read_text(encoding="utf-8")
    assert 'id="factory-alerts-status" role="status" aria-live="polite"' in health
    assert 'aria-label="Selecionar todos os alertas"' in health
    assert 'aria-label="Selecionar alerta: ${healthEscapeHtml(title)}"' in health
    assert "Reconhecer selecionadas" in health
    assert 'id="factory-alerts-ack-selected"' in health
    # Escaping stays in place for every rendered notification field.
    assert health.count("healthEscapeHtml(") >= 8


def test_health_js_stays_read_only_and_ops_script_loads_after_it() -> None:
    health = HEALTH_JS.read_text(encoding="utf-8")
    assert not re.search(r'method\s*:\s*["\'](POST|PATCH|DELETE|PUT)["\']', health, re.IGNORECASE)
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/health.js") < html.index("/static/health_ops.js")


def test_no_new_notification_types_or_data_structure_changes() -> None:
    models = (REPO_ROOT / "core" / "notifications" / "models.py").read_text(encoding="utf-8")
    assert "acknowledged: bool" in models  # existing field, untouched by this ticket
    api = (REPO_ROOT / "hub" / "backend" / "api.py").read_text(encoding="utf-8")
    assert api.count('"/notifications/{notification_id}/acknowledge"') == 1


# ---------------------------------------------------------------------------
# Backend contract used by the frontend (per-item endpoint, no new endpoint)
# ---------------------------------------------------------------------------


def _event(notification_id: str) -> NotificationEvent:
    return NotificationEvent(
        notification_id=notification_id,
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.WARNING,
        title=f"Alerta {notification_id}",
        message="Consumo elevado.",
        provider_id="deepseek",
        remaining_percent=18.0,
    )


def test_backend_acknowledge_many_items_one_by_one(tmp_path: Path) -> None:
    store = NotificationStore(store_path=tmp_path / "usr61.jsonl")
    for nid in ("n1", "n2", "n3"):
        store.add_notification(_event(nid))

    client = TestClient(app)
    headers = {"X-Hub-Session": get_hub_service().session_token}
    with patch("core.notifications.store.NotificationStore", return_value=store):
        for nid in ("n1", "n3"):
            resp = client.post(f"/api/notifications/{nid}/acknowledge", headers=headers)
            assert resp.status_code == 200
            assert resp.json() == {"notification_id": nid, "acknowledged": True}

        unread = client.get("/api/notifications?unread_only=true&limit=20").json()
        assert [n["notification_id"] for n in unread["notifications"]] == ["n2"]
        assert store.get_notification("n1").acknowledged is True
        assert store.get_notification("n2").acknowledged is False


def test_backend_unknown_id_answers_200_with_acknowledged_false(tmp_path: Path) -> None:
    """The frontend must treat this as a failure (kept selected), not a success."""
    store = NotificationStore(store_path=tmp_path / "usr61_missing.jsonl")
    store.add_notification(_event("known"))

    client = TestClient(app)
    headers = {"X-Hub-Session": get_hub_service().session_token}
    with patch("core.notifications.store.NotificationStore", return_value=store):
        resp = client.post("/api/notifications/ghost/acknowledge", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"notification_id": "ghost", "acknowledged": False}


def test_backend_acknowledge_requires_owner_session(tmp_path: Path) -> None:
    store = NotificationStore(store_path=tmp_path / "usr61_auth.jsonl")
    store.add_notification(_event("secured"))

    client = TestClient(app)
    with patch("core.notifications.store.NotificationStore", return_value=store):
        resp = client.post("/api/notifications/secured/acknowledge")
    assert resp.status_code in (401, 403)
    assert store.get_notification("secured").acknowledged is False
