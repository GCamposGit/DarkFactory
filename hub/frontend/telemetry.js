/**
 * DarkHub - AI Model Telemetry Controller.
 * Real-time structured observability for all AI model executions across local harnesses, Ollama, and OpenRouter.
 */

const telemetryState = {
  loading: false,
  autoRefresh: false,
  autoRefreshInterval: null,
  filters: {
    ticket_id: "",
    model: "",
    provider: "",
    execution_mode: "",
    limit: 25,
    offset: 0,
  },
  stats: null,
  runs: [],
  totalCount: 0,
  tickets: [],
};

document.addEventListener("DOMContentLoaded", () => {
  const drawer = document.getElementById("telemetry-drawer");
  if (drawer) {
    drawer.addEventListener("click", (event) => {
      if (event.target === drawer) closeTelemetryDrawer();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("telemetry-drawer")?.classList.contains("hidden")) {
      closeTelemetryDrawer();
    }
  });
});

function openTelemetryDrawer() {
  const drawer = document.getElementById("telemetry-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  document.body.classList.add("overflow-hidden");
  loadTelemetryTickets();
  loadTelemetryData();
}

function closeTelemetryDrawer() {
  const drawer = document.getElementById("telemetry-drawer");
  if (drawer) drawer.classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
  stopTelemetryAutoRefresh();
}

function toggleTelemetryAutoRefresh() {
  telemetryState.autoRefresh = !telemetryState.autoRefresh;
  const btn = document.getElementById("telemetry-autorefresh-btn");
  const dot = document.getElementById("telemetry-autorefresh-dot");
  if (telemetryState.autoRefresh) {
    if (btn) btn.classList.replace("border-slate-700", "border-purple-500");
    if (dot) dot.classList.replace("bg-slate-600", "bg-emerald-400");
    telemetryState.autoRefreshInterval = setInterval(() => {
      loadTelemetryData(true);
    }, 10000);
    showToast("Auto-refresh de telemetria ativado (10s)", "info");
  } else {
    stopTelemetryAutoRefresh();
    showToast("Auto-refresh desativado", "info");
  }
}

function stopTelemetryAutoRefresh() {
  telemetryState.autoRefresh = false;
  if (telemetryState.autoRefreshInterval) {
    clearInterval(telemetryState.autoRefreshInterval);
    telemetryState.autoRefreshInterval = null;
  }
  const btn = document.getElementById("telemetry-autorefresh-btn");
  const dot = document.getElementById("telemetry-autorefresh-dot");
  if (btn) btn.classList.replace("border-purple-500", "border-slate-700");
  if (dot) dot.classList.replace("bg-emerald-400", "bg-slate-600");
}

async function loadTelemetryTickets() {
  try {
    const res = await fetch("/api/telemetry/tickets");
    if (!res.ok) return;
    const tickets = await res.json();
    telemetryState.tickets = tickets;
    const select = document.getElementById("telemetry-filter-ticket");
    if (select) {
      const current = select.value;
      select.innerHTML = '<option value="">Todos os Tickets</option>' +
        tickets.map(t => `<option value="${escapeTelemetryHtml(t)}">${escapeTelemetryHtml(t)}</option>`).join("");
      select.value = current;
    }
  } catch (err) {
    console.warn("Could not load telemetry tickets:", err);
  }
}

async function loadTelemetryData(silent = false) {
  if (telemetryState.loading && !silent) return;
  telemetryState.loading = true;

  const refreshBtn = document.getElementById("telemetry-refresh-btn");
  if (refreshBtn && !silent) {
    refreshBtn.disabled = true;
    refreshBtn.classList.add("opacity-50");
  }

  const queryParams = new URLSearchParams();
  if (telemetryState.filters.ticket_id) queryParams.append("ticket_id", telemetryState.filters.ticket_id);
  if (telemetryState.filters.model) queryParams.append("model", telemetryState.filters.model);
  if (telemetryState.filters.provider) queryParams.append("provider", telemetryState.filters.provider);
  if (telemetryState.filters.execution_mode) queryParams.append("execution_mode", telemetryState.filters.execution_mode);
  queryParams.append("limit", telemetryState.filters.limit);
  queryParams.append("offset", telemetryState.filters.offset);

  const statsParams = new URLSearchParams();
  if (telemetryState.filters.ticket_id) statsParams.append("ticket_id", telemetryState.filters.ticket_id);
  if (telemetryState.filters.model) statsParams.append("model", telemetryState.filters.model);
  if (telemetryState.filters.provider) statsParams.append("provider", telemetryState.filters.provider);
  if (telemetryState.filters.execution_mode) statsParams.append("execution_mode", telemetryState.filters.execution_mode);

  try {
    const [runsRes, statsRes] = await Promise.all([
      fetch(`/api/telemetry/runs?${queryParams.toString()}`),
      fetch(`/api/telemetry/stats?${statsParams.toString()}`),
    ]);

    if (runsRes.ok) {
      const runsData = await runsRes.json();
      telemetryState.runs = runsData.runs || [];
      telemetryState.totalCount = runsData.total_count || 0;
      renderTelemetryTable();
    }

    if (statsRes.ok) {
      telemetryState.stats = await statsRes.json();
      renderTelemetryStats();
    }
  } catch (err) {
    console.error("Telemetry load error:", err);
    if (!silent) showToast("Erro ao carregar dados de telemetria", "error");
  } finally {
    telemetryState.loading = false;
    if (refreshBtn) {
      refreshBtn.disabled = false;
      refreshBtn.classList.remove("opacity-50");
    }
  }
}

function renderTelemetryStats() {
  const s = telemetryState.stats;
  if (!s) return;

  const totalRunsEl = document.getElementById("telemetry-stat-total-runs");
  const successRateEl = document.getElementById("telemetry-stat-success-rate");
  const totalTokensEl = document.getElementById("telemetry-stat-total-tokens");
  const tokenBreakdownEl = document.getElementById("telemetry-stat-token-breakdown");
  const totalCostEl = document.getElementById("telemetry-stat-total-cost");
  const avgLatencyEl = document.getElementById("telemetry-stat-avg-latency");
  const p95LatencyEl = document.getElementById("telemetry-stat-p95-latency");

  if (totalRunsEl) totalRunsEl.textContent = s.total_runs.toLocaleString();
  if (successRateEl) {
    successRateEl.textContent = `${s.success_rate_percent.toFixed(1)}% sucesso`;
    successRateEl.className = `text-[11px] font-mono ${s.success_rate_percent >= 90 ? "text-emerald-400" : s.success_rate_percent >= 75 ? "text-amber-400" : "text-rose-400"}`;
  }

  if (totalTokensEl) totalTokensEl.textContent = s.total_tokens.toLocaleString();
  if (tokenBreakdownEl) {
    tokenBreakdownEl.innerHTML = `
      <span title="Input / Prompt">${s.total_input_tokens.toLocaleString()} in</span> · 
      <span title="Processamento / Reasoning / Cache" class="text-indigo-400">${s.total_processing_tokens.toLocaleString()} proc</span> · 
      <span title="Output / Completion">${s.total_output_tokens.toLocaleString()} out</span>
    `;
  }

  if (totalCostEl) totalCostEl.textContent = `$${s.total_cost_usd.toFixed(4)}`;
  if (avgLatencyEl) avgLatencyEl.textContent = `${s.avg_latency_ms.toFixed(0)} ms`;
  if (p95LatencyEl) p95LatencyEl.textContent = `p95: ${s.p95_latency_ms.toFixed(0)} ms`;
}

function renderTelemetryTable() {
  const tbody = document.getElementById("telemetry-runs-tbody");
  const paginationInfo = document.getElementById("telemetry-pagination-info");
  const prevBtn = document.getElementById("telemetry-page-prev");
  const nextBtn = document.getElementById("telemetry-page-next");

  if (!tbody) return;

  const runs = telemetryState.runs;
  const total = telemetryState.totalCount;
  const limit = telemetryState.filters.limit;
  const offset = telemetryState.filters.offset;

  if (paginationInfo) {
    const start = total === 0 ? 0 : offset + 1;
    const end = Math.min(offset + limit, total);
    paginationInfo.textContent = `Exibindo ${start}-${end} de ${total} execuções`;
  }

  if (prevBtn) prevBtn.disabled = offset === 0;
  if (nextBtn) nextBtn.disabled = offset + limit >= total;

  if (!runs.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" class="p-8 text-center text-xs text-slate-500">
          Nenhuma execução registrada para os filtros selecionados.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = runs.map((run) => {
    const dt = new Date(run.timestamp);
    const dateFormatted = !isNaN(dt.getTime())
      ? dt.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : run.timestamp;

    const ticketBadge = run.ticket_id
      ? `<span class="px-2 py-0.5 rounded-md bg-cyan-500/10 border border-cyan-500/30 text-[10px] font-mono font-medium text-cyan-300">${escapeTelemetryHtml(run.ticket_id)}</span>`
      : `<span class="text-slate-600 text-[10px] font-mono">—</span>`;

    const modeBadge = run.execution_mode === "ui"
      ? `<span class="px-1.5 py-0.5 rounded bg-indigo-500/15 border border-indigo-500/30 text-[9px] font-mono text-indigo-300">UI</span>`
      : `<span class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-[9px] font-mono text-slate-400">Headless</span>`;

    const providerColor = run.provider === "ollama" ? "text-emerald-400" : run.provider === "openrouter" ? "text-cyan-400" : "text-amber-400";

    const statusBadge = run.success
      ? `<span class="px-1.5 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-[10px] text-emerald-300 font-medium">Sucesso</span>`
      : `<span class="px-1.5 py-0.5 rounded-full bg-rose-500/10 border border-rose-500/30 text-[10px] text-rose-300 font-medium" title="${escapeTelemetryHtml(run.error_message || 'Erro desconhecido')}">Falha ⚠️</span>`;

    const latencyDisplay = run.latency_ms >= 1000
      ? `${(run.latency_ms / 1000).toFixed(2)}s`
      : `${run.latency_ms.toFixed(0)}ms`;

    return `
      <tr class="border-b border-slate-800/60 hover:bg-slate-900/40 transition-colors text-xs">
        <td class="px-4 py-3 font-mono text-slate-400 whitespace-nowrap">${dateFormatted}</td>
        <td class="px-4 py-3 whitespace-nowrap">${ticketBadge}</td>
        <td class="px-4 py-3">
          <div class="font-medium text-slate-200 truncate max-w-[220px]" title="${escapeTelemetryHtml(run.model)}">
            ${escapeTelemetryHtml(run.model)}
          </div>
          <div class="text-[10px] font-mono ${providerColor}">
            ${escapeTelemetryHtml(run.provider)} · <span class="text-slate-500">${escapeTelemetryHtml(run.tier)}</span>
          </div>
        </td>
        <td class="px-4 py-3 whitespace-nowrap">
          <div class="flex items-center gap-1.5">
            ${modeBadge}
            <span class="text-[10px] font-mono text-slate-400 truncate max-w-[140px]" title="${escapeTelemetryHtml(run.accelerator)} on ${escapeTelemetryHtml(run.device_name)}">
              ${escapeTelemetryHtml(run.accelerator.split('(')[0].trim())}
            </span>
          </div>
        </td>
        <td class="px-4 py-3 font-mono text-slate-300 whitespace-nowrap">${latencyDisplay}</td>
        <td class="px-4 py-3 font-mono whitespace-nowrap">
          <div class="text-slate-200 font-medium">${run.total_tokens.toLocaleString()}</div>
          <div class="text-[9px] text-slate-500">
            ${run.input_tokens.toLocaleString()} in / <span class="text-indigo-400">${run.processing_tokens.toLocaleString()} proc</span> / ${run.output_tokens.toLocaleString()} out
          </div>
        </td>
        <td class="px-4 py-3 font-mono text-slate-300 whitespace-nowrap">
          ${run.cost_usd > 0 ? `$${run.cost_usd.toFixed(4)}` : `<span class="text-emerald-400">$0.00</span>`}
        </td>
        <td class="px-4 py-3 whitespace-nowrap text-right">${statusBadge}</td>
      </tr>
    `;
  }).join("");
}

function applyTelemetryFilters() {
  const ticketSelect = document.getElementById("telemetry-filter-ticket");
  const modelInput = document.getElementById("telemetry-filter-model");
  const providerSelect = document.getElementById("telemetry-filter-provider");
  const modeSelect = document.getElementById("telemetry-filter-mode");

  telemetryState.filters.ticket_id = ticketSelect ? ticketSelect.value : "";
  telemetryState.filters.model = modelInput ? modelInput.value.trim() : "";
  telemetryState.filters.provider = providerSelect ? providerSelect.value : "";
  telemetryState.filters.execution_mode = modeSelect ? modeSelect.value : "";
  telemetryState.filters.offset = 0;

  loadTelemetryData();
}

function resetTelemetryFilters() {
  const ticketSelect = document.getElementById("telemetry-filter-ticket");
  const modelInput = document.getElementById("telemetry-filter-model");
  const providerSelect = document.getElementById("telemetry-filter-provider");
  const modeSelect = document.getElementById("telemetry-filter-mode");

  if (ticketSelect) ticketSelect.value = "";
  if (modelInput) modelInput.value = "";
  if (providerSelect) providerSelect.value = "";
  if (modeSelect) modeSelect.value = "";

  telemetryState.filters = {
    ticket_id: "",
    model: "",
    provider: "",
    execution_mode: "",
    limit: 25,
    offset: 0,
  };

  loadTelemetryData();
}

function changeTelemetryPage(delta) {
  const newOffset = telemetryState.filters.offset + (delta * telemetryState.filters.limit);
  if (newOffset < 0 || newOffset >= telemetryState.totalCount) return;
  telemetryState.filters.offset = newOffset;
  loadTelemetryData();
}

function exportTelemetryJSON() {
  if (!telemetryState.runs.length) {
    showToast("Nenhum dado para exportar.", "warning");
    return;
  }
  const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(telemetryState.runs, null, 2));
  const downloadAnchor = document.createElement("a");
  downloadAnchor.setAttribute("href", dataStr);
  downloadAnchor.setAttribute("download", `telemetry_export_${new Date().toISOString().slice(0, 10)}.json`);
  document.body.appendChild(downloadAnchor);
  downloadAnchor.click();
  downloadAnchor.remove();
  showToast("Exportação concluída com sucesso!", "success");
}

function escapeTelemetryHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
