/**
 * DarkHub — Factory Health shared helpers + Home Alert Strip (DH-01, USR-43).
 *
 * Read-only surfaces only: this file and the block it powers in infra.js never
 * issue a POST/PATCH/DELETE. Every fetch here uses AbortController with a 12s
 * timeout and independent Promise.allSettled semantics where multiple sources
 * are combined, so one failing source never blanks another.
 */

const FACTORY_HEALTH_TIMEOUT_MS = 12000;
const FACTORY_HEALTH_POLL_MS = 60000;

const FACTORY_ALERTS_SUCCESS_MS = 3000;
const FACTORY_ALERTS_ERROR_MS = 6000;

const factoryAlertsState = {
  loading: false,
  refreshQueued: false,
  items: [],
  selected: new Set(),
  busy: new Set(),
  batchBusy: false,
  statusTimer: null,
};

function initFactoryAlerts() {
  ensureAlertsStripMounted();
  loadFactoryAlerts();
  healthStartVisibilityPolling(loadFactoryAlerts, FACTORY_HEALTH_POLL_MS);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initFactoryAlerts);
} else {
  initFactoryAlerts();
}

/** Escapes a value for safe innerHTML insertion (mirrors tasksEscapeHtml in tasks.js). */
function healthEscapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/**
 * Complete Portuguese relative-age phrase from an ISO timestamp: "agora",
 * "há 3min", "há 2h", "há 5d". Returns null (not a placeholder string) when
 * the timestamp is missing or invalid, so callers build their own "unknown"
 * wording instead of concatenating a broken sentence (e.g. "observado null").
 */
function healthRelativeAge(timestamp) {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.max(0, Math.round(diffMs / 1000));
  if (diffSec < 5) return "agora";
  if (diffSec < 60) return `há ${diffSec}s`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `há ${diffMin}min`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `há ${diffHour}h`;
  const diffDay = Math.round(diffHour / 24);
  return `há ${diffDay}d`;
}

/**
 * Full "observed at" sentence for a health card footer, built from a
 * healthRelativeAge() result: "observado agora", "observado há 3min", or
 * "observação sem horário" when the age could not be determined.
 */
function healthObservedPhrase(age) {
  return age ? `observado ${age}` : "observação sem horário";
}

/** GET-only JSON fetch with a hard 12s timeout via AbortController. Never mutates. */
async function healthFetchJson(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FACTORY_HEALTH_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Registers a callback that runs immediately and then on a fixed interval,
 * but only while the tab is visible; pauses while hidden and refreshes right
 * away when the tab becomes visible again.
 */
function healthStartVisibilityPolling(callback, intervalMs) {
  setInterval(() => {
    if (document.visibilityState === "visible") callback();
  }, intervalMs);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") callback();
  });
}

function healthSeverityMeta(severity) {
  const map = {
    critical: { border: "border-rose-500/40", bg: "bg-rose-500/10", text: "text-rose-200", dot: "bg-rose-400", label: "Crítico" },
    warning: { border: "border-amber-500/40", bg: "bg-amber-500/10", text: "text-amber-200", dot: "bg-amber-400", label: "Aviso" },
    info: { border: "border-indigo-500/40", bg: "bg-indigo-500/10", text: "text-indigo-200", dot: "bg-indigo-400", label: "Info" },
  };
  return map[severity] || map.info;
}

function ensureAlertsStripMounted() {
  if (document.getElementById("factory-alerts-strip")) return;
  const main = document.querySelector("main");
  if (!main) return;
  const anchor = document.getElementById("tasks-dashboard-section");

  const strip = document.createElement("section");
  strip.id = "factory-alerts-strip";
  strip.className = "hidden space-y-2";
  strip.setAttribute("aria-label", "Alertas não lidos da fábrica");
  strip.innerHTML = `
    <div id="factory-alerts-toolbar" class="flex flex-wrap items-center justify-between gap-2 px-1">
      <label class="flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer">
        <input type="checkbox" id="factory-alerts-select-all" aria-label="Selecionar todos os alertas" onchange="toggleAllFactoryAlerts(this.checked)" class="rounded border-slate-700 bg-slate-900 text-indigo-600" />
        <span>Selecionar todos</span>
        <span id="factory-alerts-selected-count" class="font-mono text-slate-500">0 selecionados</span>
      </label>
      <button type="button" id="factory-alerts-ack-selected" disabled onclick="acknowledgeSelectedNotifications()" class="text-[10px] font-mono px-2 py-1 rounded-lg border border-slate-700 bg-slate-900/80 hover:bg-slate-800 text-slate-300 hover:text-white transition active:scale-95 shadow-sm disabled:opacity-40 disabled:cursor-not-allowed disabled:active:scale-100">Reconhecer selecionadas</button>
    </div>
    <div id="factory-alerts-status" role="status" aria-live="polite" aria-atomic="true" class="hidden rounded-xl border px-3 py-2 text-xs"></div>
    <div id="factory-alerts-list" class="space-y-2"></div>`;

  if (anchor) {
    anchor.insertAdjacentElement("beforebegin", strip);
  } else {
    main.insertBefore(strip, main.firstChild);
  }
}

async function loadFactoryAlerts() {
  if (factoryAlertsState.loading) {
    // A refresh requested mid-flight (e.g. right after an acknowledge) must not be dropped.
    factoryAlertsState.refreshQueued = true;
    return;
  }
  factoryAlertsState.loading = true;
  try {
    const payload = await healthFetchJson("/api/notifications?unread_only=true&limit=20");
    const items = Array.isArray(payload?.notifications) ? payload.notifications : [];
    renderFactoryAlerts(items);
  } catch (error) {
    console.warn("Falha ao carregar alertas da fábrica:", error);
  } finally {
    factoryAlertsState.loading = false;
    if (factoryAlertsState.refreshQueued) {
      factoryAlertsState.refreshQueued = false;
      loadFactoryAlerts();
    }
  }
}

/**
 * Keeps only the selected ids that still exist in the freshly loaded list, so a
 * poll never leaves a stale (already acknowledged elsewhere) id selected.
 */
function healthReconcileSelection(selected, items) {
  const present = new Set((items || []).map((n) => healthNotificationId(n)).filter(Boolean));
  const next = new Set();
  for (const id of selected || []) {
    if (present.has(id)) next.add(id);
  }
  return next;
}

function healthNotificationId(notification) {
  const n = notification || {};
  return String(n.notification_id || n.id || "");
}

/** Builds the success/partial/failure summary for an acknowledge batch. */
function healthAckSummary(acknowledgedCount, failedCount) {
  if (failedCount === 0) {
    const text = acknowledgedCount === 1 ? "Alerta reconhecido." : `${acknowledgedCount} alertas reconhecidos.`;
    return { kind: "success", text };
  }
  if (acknowledgedCount === 0) {
    const text = failedCount === 1 ? "Falha ao reconhecer o alerta." : `Falha ao reconhecer ${failedCount} alertas.`;
    return { kind: "error", text };
  }
  return {
    kind: "error",
    text: `${acknowledgedCount} reconhecidos; ${failedCount} falharam e continuam selecionados.`,
  };
}

function renderFactoryAlerts(items) {
  factoryAlertsState.items = Array.isArray(items) ? items : [];
  factoryAlertsState.selected = healthReconcileSelection(factoryAlertsState.selected, factoryAlertsState.items);
  renderFactoryAlertsList();
}

function renderFactoryAlertsList() {
  const list = document.getElementById("factory-alerts-list");
  if (!document.getElementById("factory-alerts-strip") || !list) return;
  const items = factoryAlertsState.items;

  if (!items.length) {
    list.innerHTML = "";
  } else {
    const severityOrder = { critical: 0, warning: 1, info: 2 };
    const sorted = [...items].sort((a, b) => (severityOrder[a?.severity] ?? 3) - (severityOrder[b?.severity] ?? 3));
    list.innerHTML = sorted.map(renderFactoryAlertItem).join("");
  }
  syncFactoryAlertsUi();
}

/**
 * Updates strip visibility, toolbar and checkbox state in place (no list
 * re-render, so keyboard focus on a checkbox survives a toggle).
 */
function syncFactoryAlertsUi() {
  const strip = document.getElementById("factory-alerts-strip");
  if (!strip) return;
  const state = factoryAlertsState;
  const total = state.items.length;
  const status = document.getElementById("factory-alerts-status");
  const statusVisible = !!status && !status.classList.contains("hidden");

  // Stays visible while a status message is on screen so the user sees the
  // confirmation even after the last alert was acknowledged.
  strip.classList.toggle("hidden", total === 0 && !statusVisible);
  const toolbar = document.getElementById("factory-alerts-toolbar");
  if (toolbar) toolbar.classList.toggle("hidden", total === 0);

  const count = state.selected.size;
  const countEl = document.getElementById("factory-alerts-selected-count");
  if (countEl) countEl.textContent = count === 1 ? "1 selecionado" : `${count} selecionados`;

  const batchBtn = document.getElementById("factory-alerts-ack-selected");
  if (batchBtn) {
    batchBtn.disabled = count === 0 || state.batchBusy;
    batchBtn.textContent = state.batchBusy ? "Reconhecendo..." : "Reconhecer selecionadas";
  }

  const all = document.getElementById("factory-alerts-select-all");
  if (all) {
    all.checked = total > 0 && count === total;
    all.indeterminate = count > 0 && count < total;
    all.disabled = state.batchBusy;
  }

  document.querySelectorAll("#factory-alerts-list input[data-alert-select]").forEach((box) => {
    box.checked = state.selected.has(box.dataset.nid);
    box.disabled = state.batchBusy || state.busy.has(box.dataset.nid);
  });
}

function toggleFactoryAlertSelection(notificationId, checked) {
  if (!notificationId) return;
  if (checked) factoryAlertsState.selected.add(notificationId);
  else factoryAlertsState.selected.delete(notificationId);
  syncFactoryAlertsUi();
}

function toggleAllFactoryAlerts(checked) {
  const state = factoryAlertsState;
  state.selected = checked
    ? new Set(state.items.map((n) => healthNotificationId(n)).filter(Boolean))
    : new Set();
  syncFactoryAlertsUi();
}

/**
 * Shows a transient message in the aria-live status region. Success messages
 * disappear on their own after 3 seconds; errors stay a bit longer.
 */
function showFactoryAlertsStatus(kind, text) {
  const status = document.getElementById("factory-alerts-status");
  const state = factoryAlertsState;
  if (!status) return;
  if (state.statusTimer) clearTimeout(state.statusTimer);

  const ok = kind === "success";
  status.className = ok
    ? "rounded-xl border px-3 py-2 text-xs border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
    : "rounded-xl border px-3 py-2 text-xs border-rose-800/60 bg-rose-950/40 text-rose-300";
  const strip = document.getElementById("factory-alerts-strip");
  if (strip) strip.classList.remove("hidden");
  status.textContent = text;

  state.statusTimer = setTimeout(() => {
    state.statusTimer = null;
    status.textContent = "";
    status.className = "hidden rounded-xl border px-3 py-2 text-xs";
    syncFactoryAlertsUi();
  }, ok ? FACTORY_ALERTS_SUCCESS_MS : FACTORY_ALERTS_ERROR_MS);
}

/** Drops acknowledged ids from the local list and selection, then re-renders. */
function applyFactoryAlertsAcknowledged(ids) {
  const done = new Set(ids || []);
  const state = factoryAlertsState;
  state.items = state.items.filter((n) => !done.has(healthNotificationId(n)));
  for (const id of done) state.selected.delete(id);
  renderFactoryAlertsList();
}

function renderFactoryAlertItem(notification) {
  const n = notification || {};
  const meta = healthSeverityMeta(n.severity);
  const channels = Array.isArray(n.delivered_channels) ? n.delivered_channels.filter(Boolean) : [];
  const channelLabel = channels.length ? channels.join(", ") : (n.provider_id || "");
  const age = healthRelativeAge(n.timestamp) || "sem horário";
  const title = n.title || n.message || "Notificação";
  const nid = healthNotificationId(n);
  const checked = nid && factoryAlertsState.selected.has(nid) ? "checked" : "";
  const locked = factoryAlertsState.batchBusy || factoryAlertsState.busy.has(nid);

  return `<div class="flex items-start justify-between gap-3 rounded-xl border ${meta.border} ${meta.bg} px-3 py-2.5">
    <div class="flex items-start gap-3 min-w-0 flex-1">
      ${nid ? `<input type="checkbox" data-alert-select data-nid="${healthEscapeHtml(nid)}" ${checked} ${locked ? "disabled" : ""} aria-label="Selecionar alerta: ${healthEscapeHtml(title)}" onchange="toggleFactoryAlertSelection(this.dataset.nid, this.checked)" class="mt-0.5 shrink-0 rounded border-slate-700 bg-slate-900 text-indigo-600" />` : ""}
      <span class="mt-1 h-2 w-2 shrink-0 rounded-full ${meta.dot}" aria-hidden="true"></span>
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <p class="text-xs font-semibold ${meta.text} truncate">${healthEscapeHtml(title)}</p>
          <span class="shrink-0 text-[10px] font-mono text-slate-500" title="${healthEscapeHtml(n.timestamp || "")}">${healthEscapeHtml(meta.label)} · ${healthEscapeHtml(age)}</span>
        </div>
        ${n.message && n.message !== title ? `<p class="mt-0.5 text-[11px] text-slate-400">${healthEscapeHtml(n.message)}</p>` : ""}
        ${channelLabel ? `<p class="mt-0.5 text-[10px] font-mono text-slate-500">Canal: ${healthEscapeHtml(channelLabel)}</p>` : ""}
      </div>
    </div>
    ${nid ? `<button type="button" data-nid="${healthEscapeHtml(nid)}" ${locked ? "disabled" : ""} aria-label="Reconhecer alerta: ${healthEscapeHtml(title)}" onclick="acknowledgeNotification(this.dataset.nid)" class="shrink-0 text-[10px] font-mono px-2 py-1 rounded-lg border border-slate-700 bg-slate-900/80 hover:bg-slate-800 text-slate-300 hover:text-white transition active:scale-95 shadow-sm disabled:opacity-40 disabled:cursor-not-allowed">Reconhecer</button>` : ""}
  </div>`;
}

