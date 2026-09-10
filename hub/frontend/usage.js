/** AI account quotas and project model-call ledger for DarkHub. */

const USAGE_EXPANDED_KEY = "darkfac_usage_cards_expanded";

const usageState = {
  accounts: null,
  models: null,
  loading: false,
};

function isUsageExpanded() {
  try {
    return localStorage.getItem(USAGE_EXPANDED_KEY) === "true";
  } catch (_) {
    return false;
  }
}

function setUsageExpanded(val) {
  try {
    localStorage.setItem(USAGE_EXPANDED_KEY, val ? "true" : "false");
  } catch (_) {}
}

function toggleUsageExpansion() {
  setUsageExpanded(!isUsageExpanded());
  renderAccountUsage();
}

document.addEventListener("DOMContentLoaded", () => {
  mountAIUsageMonitor();
  loadAIUsage(false);
});

function mountAIUsageMonitor() {
  if (document.getElementById("ai-usage-section")) return;
  const quickDock = document.getElementById("quick-dock-section");
  if (!quickDock) return;
  const section = document.createElement("section");
  section.id = "ai-usage-section";
  section.className = "space-y-4 rounded-2xl border border-slate-800/90 bg-slate-900/35 p-4 sm:p-5";
  section.innerHTML = `
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div class="flex items-center gap-2">
          <span class="text-lg">◫</span>
          <h2 class="text-sm font-semibold text-white">AI Account Monitor</h2>
          <span id="usage-health-pill" class="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 text-[10px] font-mono text-slate-400">carregando</span>
        </div>
        <p class="mt-1 text-[11px] text-slate-500">Cotas reais quando a plataforma as expõe; conexão e evidência honesta quando não expõe.</p>
      </div>
      <div class="flex items-center gap-2">
        <button id="toggle-usage-expand-header" type="button" class="hidden sm:inline-flex items-center gap-1 rounded-lg border border-slate-700/80 bg-slate-800/60 px-2.5 py-1.5 text-[11px] font-medium text-slate-300 transition hover:border-slate-600 hover:text-white" title="Expandir ou recolher provedores">
          <span id="toggle-usage-expand-header-icon" class="font-bold text-indigo-400">+</span>
          <span id="toggle-usage-expand-header-text">Expandir</span>
        </button>
        <button id="refresh-ai-usage" type="button" class="rounded-lg border border-indigo-500/30 bg-indigo-500/10 px-3 py-1.5 text-[11px] font-medium text-indigo-300 transition hover:bg-indigo-500/20">
          Atualizar contas
        </button>
      </div>
    </div>
    <div id="account-usage-cards" class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      ${usageSkeleton(3)}
    </div>
    <div id="account-usage-toggle-footer" class="hidden flex items-center justify-center pt-1">
      <button id="toggle-usage-expand-footer" type="button" class="inline-flex items-center gap-1.5 rounded-xl border border-slate-800 bg-slate-950/60 px-4 py-2 text-[11px] font-medium text-slate-400 shadow-sm transition hover:border-slate-700 hover:bg-slate-900 hover:text-slate-200 active:scale-95">
        <span id="toggle-usage-expand-footer-icon" class="text-sm font-bold text-indigo-400">+</span>
        <span id="toggle-usage-expand-footer-text">Ver mais provedores</span>
      </button>
    </div>
    <div class="border-t border-slate-800/80 pt-4">
      <div class="mb-3 flex items-center justify-between">
        <div>
          <h3 class="text-xs font-semibold text-slate-200">Model Ledger</h3>
          <p class="text-[10px] text-slate-500">Chamadas por modelo, tier, harness e modalidade neste projeto.</p>
        </div>
        <div id="model-ledger-summary" class="text-right text-[10px] font-mono text-slate-500">sem eventos</div>
      </div>
      <div id="model-usage-list" class="overflow-hidden rounded-xl border border-slate-800 bg-slate-950/55">
        <div class="p-4 text-center text-[11px] text-slate-500">Carregando telemetria…</div>
      </div>
    </div>`;
  quickDock.insertAdjacentElement("afterend", section);
  document.getElementById("refresh-ai-usage")?.addEventListener("click", () => loadAIUsage(true));
  document.getElementById("toggle-usage-expand-header")?.addEventListener("click", toggleUsageExpansion);
  document.getElementById("toggle-usage-expand-footer")?.addEventListener("click", toggleUsageExpansion);
}

function usageSkeleton(count) {
  return Array.from({ length: count }, () => `
    <div class="h-28 animate-pulse rounded-xl border border-slate-800 bg-slate-950/55 p-4">
      <div class="h-3 w-32 rounded bg-slate-800"></div>
      <div class="mt-4 h-2 w-full rounded bg-slate-800"></div>
      <div class="mt-3 h-2 w-2/3 rounded bg-slate-800"></div>
    </div>`).join("");
}

async function loadAIUsage(force = false) {
  if (usageState.loading) return;
  usageState.loading = true;
  const button = document.getElementById("refresh-ai-usage");
  if (button) {
    button.disabled = true;
    button.textContent = "Atualizando…";
  }
  try {
    const accountRequest = force
      ? fetch("/api/usage/accounts/refresh", { method: "POST" })
      : fetch("/api/usage/accounts");
    const [accountsResponse, modelsResponse] = await Promise.all([
      accountRequest,
      fetch("/api/usage/models"),
    ]);
    if (!accountsResponse.ok || !modelsResponse.ok) throw new Error("usage endpoint unavailable");
    usageState.accounts = await accountsResponse.json();
    usageState.models = await modelsResponse.json();
    renderAccountUsage();
    renderModelUsage();
  } catch (error) {
    renderUsageUnavailable();
    console.warn("AI usage monitor unavailable", error);
  } finally {
    usageState.loading = false;
    if (button) {
      button.disabled = false;
      button.textContent = "Atualizar contas";
    }
  }
}

function renderAccountUsage() {
  const container = document.getElementById("account-usage-cards");
  const report = usageState.accounts;
  if (!container || !report) return;

  const priority = { openai: 0, xai: 1, google: 2, ollama: 3 };
  const allAccounts = [...(report.accounts || [])].sort((left, right) =>
    (priority[left.provider_id] ?? 10) - (priority[right.provider_id] ?? 10));

  // Determine primary priority/active accounts vs secondary/disconnected
  const isPrimary = (account) => {
    const isTopPriority = ["openai", "xai", "google"].includes(account.provider_id);
    const isActive = account.status === "connected" || account.status === "limited";
    return isTopPriority || isActive;
  };

  const primaryAccounts = allAccounts.filter(isPrimary);
  const primaryIds = new Set(primaryAccounts.map((a) => a.provider_id));
  allAccounts.slice(0, 3).forEach((a) => primaryIds.add(a.provider_id));
  const effectivePrimary = allAccounts.filter((a) => primaryIds.has(a.provider_id));
  const otherAccounts = allAccounts.filter((a) => !primaryIds.has(a.provider_id));

  const expanded = isUsageExpanded();
  const visibleAccounts = expanded || otherAccounts.length === 0 ? allAccounts : effectivePrimary;

  container.innerHTML = visibleAccounts.map(renderAccountCard).join("");

  // Update header and footer toggle controls
  const footerContainer = document.getElementById("account-usage-toggle-footer");
  const headerBtn = document.getElementById("toggle-usage-expand-header");
  const headerIcon = document.getElementById("toggle-usage-expand-header-icon");
  const headerText = document.getElementById("toggle-usage-expand-header-text");
  const footerIcon = document.getElementById("toggle-usage-expand-footer-icon");
  const footerText = document.getElementById("toggle-usage-expand-footer-text");

  if (otherAccounts.length > 0) {
    if (footerContainer) footerContainer.classList.remove("hidden");
    if (headerBtn) headerBtn.classList.remove("hidden");

    const iconStr = expanded ? "−" : "+";
    const textStr = expanded
      ? "Recolher outros provedores"
      : `Ver mais provedores (${otherAccounts.length})`;

    if (headerIcon) headerIcon.textContent = iconStr;
    if (headerText) headerText.textContent = expanded ? "Recolher" : `Mais (${otherAccounts.length})`;
    if (footerIcon) footerIcon.textContent = iconStr;
    if (footerText) footerText.textContent = textStr;
  } else {
    if (footerContainer) footerContainer.classList.add("hidden");
    if (headerBtn) headerBtn.classList.add("hidden");
  }

  const pill = document.getElementById("usage-health-pill");
  if (pill) {
    pill.textContent = `${report.connected_count || 0} ativas · ${report.limited_count || 0} limitadas`;
    pill.className = report.limited_count
      ? "rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] font-mono text-amber-300"
      : "rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-mono text-emerald-300";
  }
}

function renderAccountCard(account) {
  const styles = usageStatusStyle(account.status);
  const windows = [...(account.windows || [])].sort((a, b) => (b.window_duration_minutes || 0) - (a.window_duration_minutes || 0));
  const quota = windows.length
    ? windows.map((window) => renderQuotaWindow(window)).join("")
    : `<div class="rounded-lg border border-dashed border-slate-800 px-3 py-2 text-[10px] text-slate-500">Percentual indisponível — não interpretado como 0%.</div>`;
  const plan = account.plan ? `<span class="text-slate-500">${usageEscapeHtml(account.plan)}</span>` : "";
  return `
    <article class="rounded-xl border ${styles.border} bg-slate-950/65 p-3.5" data-provider="${usageEscapeHtml(account.provider_id)}">
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="text-xs font-semibold text-slate-100">${usageEscapeHtml(account.provider_name)}</div>
          <div class="mt-0.5 flex gap-2 text-[9px] font-mono uppercase tracking-wide text-slate-600">
            <span>${usageEscapeHtml(account.family)}</span>${plan}
          </div>
        </div>
        <span class="rounded-full px-2 py-0.5 text-[9px] font-mono ${styles.badge}">${styles.label}</span>
      </div>
      <div class="mt-3 space-y-2">${quota}</div>
      <div class="mt-3 flex items-end justify-between gap-3">
        <p class="text-[10px] leading-4 text-slate-500">${usageEscapeHtml(account.message)}</p>
        <a href="${usageEscapeHtml(account.dashboard_url || "#")}" target="_blank" rel="noopener noreferrer" class="shrink-0 text-[9px] font-mono text-indigo-400 hover:text-indigo-300">painel ↗</a>
      </div>
    </article>`;
}

function renderQuotaWindow(window) {
  const hasUsed = Number.isFinite(window.used_percent);
  const hasRemaining = Number.isFinite(window.remaining_percent);
  const remaining = hasRemaining ? Math.max(0, Math.min(100, window.remaining_percent)) : (hasUsed ? 100 - window.used_percent : null);
  const used = hasUsed ? Math.max(0, Math.min(100, window.used_percent)) : (hasRemaining ? 100 - remaining : null);

  const textColor = remaining <= 10 ? "text-rose-400" : remaining <= 30 ? "text-amber-400" : "text-emerald-400";
  const percentDisplay = remaining !== null
    ? `<span class="text-slate-400">Saldo:</span> <span class="font-semibold ${textColor}">${remaining.toFixed(0)}%</span> <span class="text-slate-500">(${used.toFixed(0)}% usado)</span>`
    : (used !== null ? `<span class="font-semibold text-slate-200">${used.toFixed(0)}% usado</span>` : "—");

  const barPercent = remaining !== null ? remaining : (used !== null ? Math.max(0, 100 - used) : 0);
  const color = barPercent <= 10 ? "bg-rose-500" : barPercent <= 30 ? "bg-amber-400" : "bg-emerald-400";
  const reset = window.resets_at ? `reset ${formatUsageReset(window.resets_at)}` : "reset não informado";

  return `
    <div>
      <div class="mb-1 flex items-center justify-between gap-2 text-[10px] font-mono">
        <span class="truncate text-slate-300 font-medium">${usageEscapeHtml(window.label)}</span>
        <div class="text-[10px] font-mono">${percentDisplay}</div>
      </div>
      <div class="h-1.5 overflow-hidden rounded-full bg-slate-800" title="${remaining !== null ? `Saldo restante: ${remaining.toFixed(1)}% | Consumo: ${used.toFixed(1)}%` : ''}">
        <div class="h-full rounded-full ${color}" style="width:${barPercent}%"></div>
      </div>
      <div class="mt-1 text-right text-[9px] text-slate-600">${usageEscapeHtml(reset)}</div>
    </div>`;
}

function renderModelUsage() {
  const list = document.getElementById("model-usage-list");
  const summary = document.getElementById("model-ledger-summary");
  const report = usageState.models;
  if (!list || !summary || !report) return;
  summary.innerHTML = `<span class="text-slate-300">${report.total_calls}</span> calls · <span class="text-slate-300">${report.total_tokens}</span> tokens · <span class="text-slate-300">$${Number(report.total_cost_usd || 0).toFixed(4)}</span>`;
  const rows = (report.aggregates || []).slice(0, 10);
  if (!rows.length) {
    list.innerHTML = `<div class="p-4 text-center text-[11px] text-slate-500">O ledger está pronto. A próxima chamada de texto, imagem ou harness aparecerá aqui.</div>`;
    return;
  }
  list.innerHTML = rows.map((row) => `
    <div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-slate-800/70 px-3 py-2.5 last:border-b-0">
      <div class="min-w-0">
        <div class="truncate text-[11px] font-medium text-slate-200">${usageEscapeHtml(row.model)}</div>
        <div class="mt-0.5 truncate text-[9px] font-mono text-slate-600">${usageEscapeHtml(row.provider)} · ${usageEscapeHtml(row.tier)} · ${usageEscapeHtml(row.harness)} · ${usageEscapeHtml(row.modality)}</div>
      </div>
      <div class="text-right">
        <div class="text-xs font-semibold text-indigo-300">${row.call_count}×</div>
        <div class="text-[9px] text-slate-600">${row.failure_count} falhas</div>
      </div>
    </div>`).join("");
}

function renderUsageUnavailable() {
  const cards = document.getElementById("account-usage-cards");
  const list = document.getElementById("model-usage-list");
  if (cards) cards.innerHTML = `<div class="col-span-full rounded-xl border border-amber-500/20 bg-amber-500/5 p-4 text-[11px] text-amber-300">Monitor temporariamente indisponível. O restante do Hub continua operacional.</div>`;
  if (list) list.innerHTML = `<div class="p-4 text-center text-[11px] text-slate-500">Ledger indisponível nesta leitura.</div>`;
}

function usageStatusStyle(status) {
  const values = {
    connected: { label: "conectada", border: "border-emerald-500/20", badge: "bg-emerald-500/10 text-emerald-300" },
    limited: { label: "limite", border: "border-amber-500/30", badge: "bg-amber-500/10 text-amber-300" },
    degraded: { label: "degradada", border: "border-rose-500/20", badge: "bg-rose-500/10 text-rose-300" },
    disconnected: { label: "não conectada", border: "border-slate-800", badge: "bg-slate-800 text-slate-500" },
    unknown: { label: "desconhecida", border: "border-slate-800", badge: "bg-slate-800 text-slate-400" },
  };
  return values[status] || values.unknown;
}

function formatUsageReset(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "não informado";
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function usageEscapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}
