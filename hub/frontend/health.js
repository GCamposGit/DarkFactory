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

const factoryAlertsState = {
  loading: false,
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
  strip.setAttribute("aria-live", "polite");
  strip.setAttribute("aria-label", "Alertas não lidos da fábrica");
  strip.innerHTML = `<div id="factory-alerts-list" class="space-y-2"></div>`;

  if (anchor) {
    anchor.insertAdjacentElement("beforebegin", strip);
  } else {
    main.insertBefore(strip, main.firstChild);
  }
}

async function loadFactoryAlerts() {
  if (factoryAlertsState.loading) return;
  factoryAlertsState.loading = true;
  try {
    const payload = await healthFetchJson("/api/notifications?unread_only=true&limit=20");
    const items = Array.isArray(payload?.notifications) ? payload.notifications : [];
    renderFactoryAlerts(items);
  } catch (error) {
    console.warn("Falha ao carregar alertas da fábrica:", error);
  } finally {
    factoryAlertsState.loading = false;
  }
}

function renderFactoryAlerts(items) {
  const strip = document.getElementById("factory-alerts-strip");
  const list = document.getElementById("factory-alerts-list");
  if (!strip || !list) return;

  if (!items.length) {
    strip.classList.add("hidden");
    list.innerHTML = "";
    return;
  }

  const severityOrder = { critical: 0, warning: 1, info: 2 };
  const sorted = [...items].sort((a, b) => (severityOrder[a?.severity] ?? 3) - (severityOrder[b?.severity] ?? 3));
  list.innerHTML = sorted.map(renderFactoryAlertItem).join("");
  strip.classList.remove("hidden");
}

function renderFactoryAlertItem(notification) {
  const n = notification || {};
  const meta = healthSeverityMeta(n.severity);
  const channels = Array.isArray(n.delivered_channels) ? n.delivered_channels.filter(Boolean) : [];
  const channelLabel = channels.length ? channels.join(", ") : (n.provider_id || "");
  const age = healthRelativeAge(n.timestamp) || "sem horário";
  const title = n.title || n.message || "Notificação";
  const nid = n.notification_id || n.id || "";

  return `<div class="flex items-start justify-between gap-3 rounded-xl border ${meta.border} ${meta.bg} px-3 py-2.5">
    <div class="flex items-start gap-3 min-w-0 flex-1">
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
    ${nid ? `<button type="button" onclick="confirmAcknowledgeNotification('${healthEscapeHtml(nid)}', '${healthEscapeHtml(title)}')" class="shrink-0 text-[10px] font-mono px-2 py-1 rounded-lg border border-slate-700 bg-slate-900/80 hover:bg-slate-800 text-slate-300 hover:text-white transition active:scale-95 shadow-sm">Reconhecer</button>` : ""}
  </div>`;
}

