/**
 * API Credits and Financial Balances ($) Monitor for DarkHub.
 * Governed by USR-12: Independent from subscription % quotas and model routing.
 */

const CREDITS_EXPANDED_KEY = "darkfac_credits_cards_expanded";

const creditsState = {
  report: null,
  loading: false,
};

function isCreditsExpanded() {
  try {
    return localStorage.getItem(CREDITS_EXPANDED_KEY) === "true";
  } catch (_) {
    return false;
  }
}

function setCreditsExpanded(val) {
  try {
    localStorage.setItem(CREDITS_EXPANDED_KEY, val ? "true" : "false");
  } catch (_) {}
}

function toggleCreditsExpansion() {
  setCreditsExpanded(!isCreditsExpanded());
  renderCredits();
}

document.addEventListener("DOMContentLoaded", () => {
  mountCreditsMonitor();
  loadCredits(false);
});

function mountCreditsMonitor() {
  if (document.getElementById("api-credits-section")) return;
  const targetAnchor = document.getElementById("ai-usage-section") || document.getElementById("quick-dock-section");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "api-credits-section";
  section.className = "space-y-4 rounded-2xl border border-emerald-900/60 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-emerald-950/10";
  section.innerHTML = `
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400 font-bold text-sm">$</span>
          <h2 class="text-sm font-semibold text-white">API Credits & Billing Monitor ($)</h2>
          <span id="credits-health-pill" class="rounded-full border border-emerald-700/60 bg-emerald-950/40 px-2.5 py-0.5 text-[10px] font-mono text-emerald-300">sincronizando</span>
        </div>
        <p class="mt-1 text-[11px] text-slate-400">
          Controle financeiro em dólares ($) de gastos no mês corrente e saldo disponível em contas de API ativas e planejadas.
        </p>
      </div>
      <div class="flex items-center gap-3">
        <div class="hidden sm:flex flex-col items-end text-right">
          <span class="text-[10px] uppercase font-mono text-slate-400">Gasto Total no Mês</span>
          <span id="credits-total-spend" class="text-xs font-bold font-mono text-emerald-400">$0.00</span>
        </div>
        <button id="toggle-credits-expand-header" type="button" class="hidden sm:inline-flex items-center gap-1 rounded-lg border border-slate-700/80 bg-slate-800/60 px-2.5 py-1.5 text-[11px] font-medium text-slate-300 transition hover:border-slate-600 hover:text-white" title="Expandir ou recolher provedores planejados">
          <span id="toggle-credits-expand-header-icon" class="font-bold text-emerald-400">+</span>
          <span id="toggle-credits-expand-header-text">Expandir</span>
        </button>
        <button id="refresh-api-credits" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-[11px] font-medium text-emerald-300 transition hover:bg-emerald-500/20 active:scale-95">
          <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Atualizar Créditos
        </button>
      </div>
    </div>
    <div id="api-credits-cards" class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      ${creditsSkeleton(3)}
    </div>
    <div id="api-credits-toggle-footer" class="hidden flex items-center justify-center pt-1">
      <button id="toggle-credits-expand-footer" type="button" class="inline-flex items-center gap-1.5 rounded-xl border border-emerald-900/40 bg-slate-950/60 px-4 py-2 text-[11px] font-medium text-slate-400 shadow-sm transition hover:border-emerald-700/60 hover:bg-slate-900 hover:text-slate-200 active:scale-95">
        <span id="toggle-credits-expand-footer-icon" class="text-sm font-bold text-emerald-400">+</span>
        <span id="toggle-credits-expand-footer-text">Ver provedores planejados</span>
      </button>
    </div>`;

  targetAnchor.insertAdjacentElement("afterend", section);
  document.getElementById("refresh-api-credits")?.addEventListener("click", () => loadCredits(true));
  document.getElementById("toggle-credits-expand-header")?.addEventListener("click", toggleCreditsExpansion);
  document.getElementById("toggle-credits-expand-footer")?.addEventListener("click", toggleCreditsExpansion);
}

function creditsSkeleton(count) {
  return Array.from({ length: count }, () => `
    <div class="h-32 animate-pulse rounded-xl border border-slate-800 bg-slate-950/60 p-4">
      <div class="flex items-center justify-between">
        <div class="h-4 w-28 rounded bg-slate-800"></div>
        <div class="h-4 w-14 rounded-full bg-slate-800"></div>
      </div>
      <div class="mt-4 grid grid-cols-2 gap-2">
        <div class="h-8 rounded bg-slate-800/80"></div>
        <div class="h-8 rounded bg-slate-800/80"></div>
      </div>
      <div class="mt-3 h-3 w-36 rounded bg-slate-800/60"></div>
    </div>`).join("");
}

async function loadCredits(force = false) {
  if (creditsState.loading) return;
  creditsState.loading = true;
  const button = document.getElementById("refresh-api-credits");
  if (button) {
    button.disabled = true;
    button.classList.add("opacity-60");
  }

  try {
    const endpoint = force ? "/api/credits/refresh" : "/api/credits";
    const method = force ? "POST" : "GET";
    const response = await fetch(endpoint, { method });
    if (!response.ok) throw new Error(`Credits API failed: ${response.status}`);
    creditsState.report = await response.json();
    renderCredits();
  } catch (error) {
    console.warn("Failed to load API credits:", error);
    renderCreditsError(error.message);
  } finally {
    creditsState.loading = false;
    if (button) {
      button.disabled = false;
      button.classList.remove("opacity-60");
    }
  }
}

function renderCredits() {
  const report = creditsState.report;
  if (!report) return;

  const totalSpendEl = document.getElementById("credits-total-spend");
  if (totalSpendEl) {
    totalSpendEl.textContent = `$${(report.total_month_spend_usd || 0).toFixed(2)}`;
  }

  const pill = document.getElementById("credits-health-pill");
  if (pill) {
    pill.textContent = "online";
    pill.className = "rounded-full border border-emerald-600 bg-emerald-950/60 px-2.5 py-0.5 text-[10px] font-mono text-emerald-300";
  }

  const container = document.getElementById("api-credits-cards");
  if (!container) return;

  const allAccounts = report.accounts || [];

  // Active default accounts: strictly the 2 active billing providers (OpenRouter and OpenAI)
  const isPrimary = (account) => {
    return ["openrouter", "openai"].includes(account.provider_id);
  };

  const primaryAccounts = allAccounts.filter(isPrimary);
  const primaryIds = new Set(primaryAccounts.map((a) => a.provider_id));
  const plannedAccounts = allAccounts.filter((a) => !primaryIds.has(a.provider_id));

  const expanded = isCreditsExpanded();
  const visibleAccounts = expanded || plannedAccounts.length === 0 ? allAccounts : primaryAccounts;

  container.innerHTML = visibleAccounts.map(renderCreditCard).join("");

  // Update toggle button states
  const footerContainer = document.getElementById("api-credits-toggle-footer");
  const headerBtn = document.getElementById("toggle-credits-expand-header");
  const headerIcon = document.getElementById("toggle-credits-expand-header-icon");
  const headerText = document.getElementById("toggle-credits-expand-header-text");
  const footerIcon = document.getElementById("toggle-credits-expand-footer-icon");
  const footerText = document.getElementById("toggle-credits-expand-footer-text");

  if (plannedAccounts.length > 0) {
    if (footerContainer) footerContainer.classList.remove("hidden");
    if (headerBtn) headerBtn.classList.remove("hidden");

    const iconStr = expanded ? "−" : "+";
    const textStr = expanded
      ? "Recolher provedores planejados"
      : `Ver provedores planejados (${plannedAccounts.length})`;

    if (headerIcon) headerIcon.textContent = iconStr;
    if (headerText) headerText.textContent = expanded ? "Recolher" : `Planejados (${plannedAccounts.length})`;
    if (footerIcon) footerIcon.textContent = iconStr;
    if (footerText) footerText.textContent = textStr;
  } else {
    if (footerContainer) footerContainer.classList.add("hidden");
    if (headerBtn) headerBtn.classList.add("hidden");
  }
}

function renderCreditCard(account) {
  const isConnected = account.is_connected;
  const isPlanning = account.status === "planning";

  let badgeClass = "border-slate-700 bg-slate-800/60 text-slate-400";
  let badgeLabel = "Desconectada";

  if (isConnected) {
    badgeClass = "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    badgeLabel = "Ativa / Conectada";
  } else if (isPlanning) {
    badgeClass = "border-indigo-500/30 bg-indigo-500/10 text-indigo-300";
    badgeLabel = "Planejada";
  }

  const spendDisplay =
    account.current_month_spend_usd !== null && account.current_month_spend_usd !== undefined
      ? `$${account.current_month_spend_usd.toFixed(2)}`
      : `<span class="text-slate-500">--</span>`;

  const creditDisplay =
    account.available_credit_usd !== null && account.available_credit_usd !== undefined
      ? `$${account.available_credit_usd.toFixed(2)}`
      : `<span class="text-slate-500">--</span>`;

  const linksHtml = (account.official_links || [])
    .map(
      (link) => `
      <a href="${link.url}" target="_blank" rel="noopener noreferrer" 
         class="inline-flex items-center gap-1 text-[11px] font-medium text-indigo-400 hover:text-indigo-300 underline underline-offset-2 transition-colors">
        <span>${link.title}</span>
        <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
        </svg>
      </a>
    `
    )
    .join("<span class='text-slate-600 text-[10px]'>•</span>");

  return `
    <div class="flex flex-col justify-between rounded-xl border ${
      isConnected ? "border-emerald-800/50 bg-slate-950/70" : "border-slate-800 bg-slate-950/40"
    } p-4 transition hover:border-slate-700">
      <div>
        <div class="flex items-center justify-between gap-2">
          <h3 class="text-xs font-semibold text-slate-100">${account.provider_name}</h3>
          <span class="rounded-md border px-2 py-0.5 text-[9px] font-medium tracking-wide uppercase ${badgeClass}">
            ${badgeLabel}
          </span>
        </div>
        
        <div class="mt-3 grid grid-cols-2 gap-2 rounded-lg bg-slate-900/60 p-2.5 border border-slate-800/60">
          <div>
            <div class="text-[9px] font-mono uppercase tracking-wider text-slate-400">Gasto no Mês</div>
            <div class="mt-0.5 text-sm font-bold font-mono text-emerald-400">${spendDisplay}</div>
          </div>
          <div>
            <div class="text-[9px] font-mono uppercase tracking-wider text-slate-400">Crédito Disponível</div>
            <div class="mt-0.5 text-sm font-bold font-mono text-sky-400">${creditDisplay}</div>
          </div>
        </div>

        ${account.notes ? `<p class="mt-2 text-[10px] text-slate-400 line-clamp-2">${account.notes}</p>` : ""}
      </div>

      <div class="mt-3 pt-2.5 border-t border-slate-800/60 flex flex-wrap items-center justify-between gap-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-[10px] font-mono text-slate-500">Portais:</span>
          ${linksHtml || '<span class="text-[10px] text-slate-500">Nenhum</span>'}
        </div>
        <button 
          type="button" 
          onclick="promptEditCredits('${account.provider_id}', ${account.current_month_spend_usd || 0}, ${account.available_credit_usd || 0})"
          class="rounded border border-slate-700 bg-slate-800/80 px-2 py-0.5 text-[9px] font-mono text-slate-300 hover:border-slate-600 hover:bg-slate-700 transition"
          title="Ajustar valores sincronizados com o dashboard oficial">
          ✎ Ajustar
        </button>
      </div>
    </div>
  `;
}

async function promptEditCredits(providerId, currentSpend, currentCredit) {
  const newSpendStr = prompt(
    `Atualizar Gasto no Mês ($) para ${providerId}:`,
    currentSpend !== undefined ? currentSpend : "0.00"
  );
  if (newSpendStr === null) return;
  const newCreditStr = prompt(
    `Atualizar Crédito Disponível ($) para ${providerId}:`,
    currentCredit !== undefined ? currentCredit : "0.00"
  );
  if (newCreditStr === null) return;

  const spendVal = parseFloat(newSpendStr);
  const creditVal = parseFloat(newCreditStr);

  try {
    const res = await fetch(`/api/credits/accounts/${providerId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_month_spend_usd: isNaN(spendVal) ? 0.0 : spendVal,
        available_credit_usd: isNaN(creditVal) ? 0.0 : creditVal,
        notes: `Atualizado manualmente via Hub (${new Date().toLocaleDateString("pt-BR")})`,
      }),
    });
    if (res.ok) {
      await loadCredits(true);
    }
  } catch (err) {
    console.error("Falha ao atualizar créditos:", err);
  }
}

function renderCreditsError(msg) {
  const pill = document.getElementById("credits-health-pill");
  if (pill) {
    pill.textContent = "indisponível";
    pill.className = "rounded-full border border-amber-700 bg-amber-950/40 px-2.5 py-0.5 text-[10px] font-mono text-amber-300";
  }
}
