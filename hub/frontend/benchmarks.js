/**
 * DarkHub - Benchmarks & Smart Routing Consultation (DH-05 / USR-52)
 *
 * Exposes Pareto efficiency frontiers, multi-domain benchmarks,
 * empirical metrics, and task routing simulation in read-only mode.
 */

const benchmarksUi = {
  activeTab: "models",
  selectedDomain: "coding",
  selectedTier: "high",
  domains: [],
  domainFrontier: null,
  proximity: null,
  speculativeTop3: [],
  empirical: null,
  routerAnalysis: null,
  frontier: null,
  loading: false,
};

function switchBenchmarkTab(tab) {
  benchmarksUi.activeTab = tab;
  const tabs = ["models", "domains", "speculative", "empirical", "router"];
  tabs.forEach((t) => {
    const btn = document.getElementById(`bench-tab-${t}`);
    const view = document.getElementById(`bench-view-${t}`);
    const isActive = t === tab;
    if (btn) {
      btn.className = isActive
        ? "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all bg-amber-500/20 text-amber-300 border border-amber-500/40 font-semibold"
        : "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all text-slate-400 hover:text-slate-200 border border-transparent";
      btn.setAttribute("aria-selected", String(isActive));
    }
    if (view) {
      view.classList.toggle("hidden", !isActive);
    }
  });

  if (tab === "models") {
    loadBenchmarkFrontier();
  } else if (tab === "domains") {
    loadBenchmarkDomains();
  } else if (tab === "speculative") {
    loadSpeculativeAndProximity();
  } else if (tab === "empirical") {
    loadEmpiricalBenchmarks();
  }
}

async function loadBenchmarkFrontier() {
  try {
    const res = await fetch("/api/benchmarks/frontier");
    if (!res.ok) return;
    const frontier = await res.json();
    benchmarksUi.frontier = frontier;
  } catch (err) {
    console.warn("Frontier consultation failed:", err);
  }
}

async function loadBenchmarkDomains() {
  const container = document.getElementById("bench-domains-container");
  if (container) container.innerHTML = '<div class="py-8 text-center text-slate-500 text-xs font-mono">Carregando domínios...</div>';
  try {
    const res = await fetch("/api/benchmarks/domains");
    if (!res.ok) throw new Error("Falha ao consultar domínios");
    benchmarksUi.domains = await res.json();
    renderBenchmarkDomains();
    loadDomainFrontier(benchmarksUi.selectedDomain);
  } catch (err) {
    if (container) container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${err.message}</div>`;
  }
}

async function loadDomainFrontier(domain) {
  benchmarksUi.selectedDomain = domain;
  const container = document.getElementById("bench-domain-frontier-results");
  if (container) container.innerHTML = '<div class="py-6 text-center text-slate-500 text-xs font-mono">Consultando fronteira do domínio...</div>';
  try {
    const res = await fetch(`/api/benchmarks/domains/${encodeURIComponent(domain)}/frontier`);
    if (!res.ok) throw new Error("Fronteira não encontrada para este domínio");
    benchmarksUi.domainFrontier = await res.json();
    renderDomainFrontier();
  } catch (err) {
    if (container) container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${err.message}</div>`;
  }
}

function renderBenchmarkDomains() {
  const select = document.getElementById("bench-domain-select");
  if (!select) return;
  select.innerHTML = benchmarksUi.domains.map((d) => {
    const key = d.domain_key || d.domain;
    const name = d.name || d.label || key;
    return `<option value="${key}" ${key === benchmarksUi.selectedDomain ? "selected" : ""}>${name}</option>`;
  }).join("");
}

function renderDomainFrontier() {
  const container = document.getElementById("bench-domain-frontier-results");
  if (!container || !benchmarksUi.domainFrontier) return;

  const frontier = benchmarksUi.domainFrontier;
  const models = frontier.models || frontier.frontier_models || [];

  if (!models.length) {
    container.innerHTML = '<div class="py-6 text-center text-slate-500 text-xs font-mono">Nenhum modelo registrado na fronteira deste domínio.</div>';
    return;
  }

  container.innerHTML = `
    <div class="space-y-3">
      <div class="flex items-center justify-between text-xs text-slate-400 font-mono border-b border-slate-800 pb-2">
        <span>Domínio: <strong class="text-amber-300 uppercase">${frontier.domain || benchmarksUi.selectedDomain}</strong></span>
        <span>${models.length} modelo(s) na fronteira Pareto</span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        ${models.map((m) => `
          <div class="p-3.5 rounded-xl border border-slate-800 bg-slate-950/60 flex flex-col justify-between space-y-2">
            <div class="flex items-start justify-between gap-2">
              <div>
                <h4 class="text-xs font-bold text-slate-100">${m.name || m.model_id}</h4>
                <span class="text-[10px] text-slate-500 font-mono">${m.model_id}</span>
              </div>
              <span class="px-2 py-0.5 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 text-[10px] font-mono font-medium">Pareto</span>
            </div>
            <div class="grid grid-cols-3 gap-2 text-[11px] font-mono pt-1 border-t border-slate-900">
              <div>
                <span class="text-slate-500 block text-[9px] uppercase">Score Domínio</span>
                <span class="text-amber-400 font-bold">${m.score ? m.score.toFixed(1) : (m.domain_score ? m.domain_score.toFixed(1) : "--")}</span>
              </div>
              <div>
                <span class="text-slate-500 block text-[9px] uppercase">Custo / 1k</span>
                <span class="text-emerald-400">${m.cost_per_task ? "$" + m.cost_per_task.toFixed(4) : (m.cost_per_1k ? "$" + m.cost_per_1k.toFixed(4) : "$0.00")}</span>
              </div>
              <div>
                <span class="text-slate-500 block text-[9px] uppercase">Velocidade</span>
                <span class="text-slate-300">${m.output_speed_tps ? m.output_speed_tps.toFixed(0) + " t/s" : "--"}</span>
              </div>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

async function loadSpeculativeAndProximity() {
  const container = document.getElementById("bench-speculative-container");
  if (container) container.innerHTML = '<div class="py-8 text-center text-slate-500 text-xs font-mono">Carregando Top-3 e Proximidade de Fronteira...</div>';
  try {
    const [top3Res, proxRes] = await Promise.all([
      fetch(`/api/benchmarks/speculative/top3?tier=${encodeURIComponent(benchmarksUi.selectedTier)}`),
      fetch("/api/benchmarks/proximity"),
    ]);

    benchmarksUi.speculativeTop3 = top3Res.ok ? await top3Res.json() : [];
    benchmarksUi.proximity = proxRes.ok ? await proxRes.json() : null;
    renderSpeculativeAndProximity();
  } catch (err) {
    if (container) container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${err.message}</div>`;
  }
}

function renderSpeculativeAndProximity() {
  const container = document.getElementById("bench-speculative-container");
  if (!container) return;

  const top3 = benchmarksUi.speculativeTop3 || [];
  const prox = benchmarksUi.proximity || {};
  const challengers = prox.challengers || prox.near_pareto || [];

  const top3Markup = top3.length
    ? top3.map((m, idx) => `
        <div class="p-3 rounded-xl border border-slate-800 bg-slate-950/60 flex items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <span class="w-6 h-6 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 flex items-center justify-center text-xs font-mono font-bold">${idx + 1}</span>
            <div>
              <div class="text-xs font-semibold text-slate-200">${m.name || m.model_id}</div>
              <div class="text-[10px] text-slate-500 font-mono">${m.model_id || ""}</div>
            </div>
          </div>
          <div class="text-right text-[11px] font-mono">
            <span class="text-amber-400 font-medium">${m.score ? m.score.toFixed(1) : "--"} pts</span> ·
            <span class="text-emerald-400">${m.cost_per_task ? "$" + m.cost_per_task.toFixed(4) : "$0.00"}</span>
          </div>
        </div>
      `).join("")
    : '<div class="text-xs text-slate-500 font-mono">Nenhum candidato retornado para este tier.</div>';

  const challengersMarkup = Array.isArray(challengers) && challengers.length
    ? challengers.map((c) => `
        <div class="p-2.5 rounded-lg border border-slate-800 bg-slate-950/40 text-xs font-mono flex items-center justify-between">
          <span class="text-slate-300">${c.name || c.model_id}</span>
          <span class="text-amber-400 font-semibold">Gap: ε=${c.epsilon !== undefined ? c.epsilon.toFixed(3) : (c.fpi !== undefined ? c.fpi.toFixed(2) : "--")}</span>
        </div>
      `).join("")
    : '<div class="text-xs text-slate-500 font-mono">Nenhum modelo na faixa de proximidade imediata.</div>';

  container.innerHTML = `
    <div class="space-y-5">
      <!-- Top 3 Speculative Section -->
      <div class="space-y-3">
        <div class="flex items-center justify-between">
          <div>
            <h4 class="text-xs font-bold uppercase tracking-wider text-slate-300 font-mono">Top-3 Especulativo em Cascata</h4>
            <p class="text-[11px] text-slate-500 font-mono">Modelos preferenciais ordenados para execução paralela de menor custo e maior acurácia.</p>
          </div>
          <select id="bench-tier-select" onchange="benchmarksUi.selectedTier=this.value; loadSpeculativeAndProximity();" class="bg-slate-950 border border-slate-800 rounded-xl px-2.5 py-1 text-xs text-slate-200 font-mono">
            <option value="high" ${benchmarksUi.selectedTier === "high" ? "selected" : ""}>Tier Alto (High)</option>
            <option value="medium" ${benchmarksUi.selectedTier === "medium" ? "selected" : ""}>Tier Médio (Medium)</option>
            <option value="low" ${benchmarksUi.selectedTier === "low" ? "selected" : ""}>Tier Rápido/Local (Low)</option>
          </select>
        </div>
        <div class="space-y-2">
          ${top3Markup}
        </div>
      </div>

      <!-- Frontier Proximity Index Section -->
      <div class="space-y-3 pt-3 border-t border-slate-800/80">
        <div>
          <h4 class="text-xs font-bold uppercase tracking-wider text-slate-300 font-mono">Índice de Proximidade à Fronteira (FPI)</h4>
          <p class="text-[11px] text-slate-500 font-mono">Modelos desafiantes (near-Pareto) com gap epsilon reduzido frente aos líderes de mercado.</p>
        </div>
        <div class="space-y-1.5">
          ${challengersMarkup}
        </div>
      </div>
    </div>
  `;
}

async function loadEmpiricalBenchmarks() {
  const container = document.getElementById("bench-empirical-container");
  if (container) container.innerHTML = '<div class="py-8 text-center text-slate-500 text-xs font-mono">Consultando resultados empíricos da fábrica...</div>';
  try {
    const res = await fetch("/api/benchmarks/empirical");
    if (!res.ok) throw new Error("Falha ao consultar benchmarks empíricos");
    benchmarksUi.empirical = await res.json();
    renderEmpiricalBenchmarks();
  } catch (err) {
    if (container) container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${err.message}</div>`;
  }
}

function renderEmpiricalBenchmarks() {
  const container = document.getElementById("bench-empirical-container");
  if (!container || !benchmarksUi.empirical) return;

  const data = benchmarksUi.empirical;
  const models = data.leaderboard || data.models || [];

  container.innerHTML = `
    <div class="space-y-4">
      <div class="p-4 rounded-xl border border-slate-800 bg-slate-950/50 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h4 class="text-xs font-bold text-white uppercase font-mono">Desempenho Empírico na Dark Factory</h4>
          <p class="text-[11px] text-slate-400 font-mono mt-0.5">Métricas colhidas em execuções reais de harness e pipelines da fábrica (Elo Bradley-Terry e Pass@1 real).</p>
        </div>
        <div class="text-right text-xs font-mono text-slate-400">
          Total de ensaios: <strong class="text-slate-100">${data.total_runs || models.length || 0}</strong>
        </div>
      </div>

      <div class="overflow-x-auto rounded-xl border border-slate-800">
        <table class="w-full text-left text-xs font-mono">
          <thead class="bg-slate-950 text-[10px] uppercase text-slate-500 border-b border-slate-800">
            <tr>
              <th class="p-3">Modelo</th>
              <th class="p-3">Pass@1 Real</th>
              <th class="p-3">Ranking Elo</th>
              <th class="p-3">Latência Média</th>
              <th class="p-3 text-right">Amostras</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60">
            ${Array.isArray(models) && models.length ? models.map((m) => `
              <tr class="hover:bg-slate-800/30">
                <td class="p-3 font-semibold text-slate-200">${m.name || m.model_id}</td>
                <td class="p-3 text-emerald-400 font-bold">${m.pass_at_1 !== undefined ? (m.pass_at_1 * 100).toFixed(1) + "%" : (m.pass_rate !== undefined ? m.pass_rate + "%" : "--")}</td>
                <td class="p-3 text-amber-300 font-bold">${m.elo !== undefined ? m.elo : "--"}</td>
                <td class="p-3 text-slate-400">${m.avg_latency ? m.avg_latency.toFixed(2) + "s" : "--"}</td>
                <td class="p-3 text-right text-slate-500">${m.runs_count || m.samples || 0}</td>
              </tr>
            `).join("") : `
              <tr>
                <td colspan="5" class="p-6 text-center text-slate-500">Nenhum resultado empírico registrado ainda no ledger.</td>
              </tr>
            `}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

async function simulateTaskRouting() {
  const reqInput = document.getElementById("bench-router-requirement");
  const compSelect = document.getElementById("bench-router-complexity");
  const resultsDiv = document.getElementById("bench-router-results");
  if (!reqInput || !resultsDiv) return;

  const requirement = reqInput.value.trim();
  if (!requirement) {
    resultsDiv.classList.remove("hidden");
    resultsDiv.innerHTML = '<div class="p-3 rounded-lg border border-amber-500/30 bg-amber-950/20 text-amber-300 text-xs font-mono">Por favor, descreva uma tarefa para simulação.</div>';
    return;
  }

  const complexity = compSelect ? compSelect.value : "high";
  resultsDiv.classList.remove("hidden");
  resultsDiv.innerHTML = '<div class="py-6 text-center text-slate-400 text-xs font-mono">Analisando intenção e classificando domínios canônicos...</div>';

  try {
    const res = await fetch("/api/benchmarks/route-task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requirement, complexity, offline: false }),
    });

    if (!res.ok) throw new Error("Falha na simulação de roteamento");
    const data = await res.json();
    renderRoutingSimulationResult(data, resultsDiv);
  } catch (err) {
    resultsDiv.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${err.message}</div>`;
  }
}

function renderRoutingSimulationResult(data, container) {
  const domain = data.detected_domain || data.selected_domain || data.domain || "coding";
  const model = data.optimal_model_name || data.optimal_model_id || data.recommended_model || "Desconhecido";
  const rationale = data.intent_explanation || data.rationale || data.justification || "Modelo ótimo localizado na fronteira de Pareto com base na complexidade e custo da tarefa.";
  const top3 = data.speculative_candidates || data.speculative_top3 || [];

  container.innerHTML = `
    <div class="p-5 rounded-2xl border border-cyan-500/30 bg-slate-950/80 space-y-4 font-mono">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div>
          <span class="text-[10px] text-slate-500 uppercase tracking-wider">Domínio Classificado</span>
          <h4 class="text-sm font-bold text-cyan-300 uppercase">${domain}</h4>
        </div>
        <div class="text-right">
          <span class="text-[10px] text-slate-500 uppercase tracking-wider">Modelo Recomendado</span>
          <div class="text-sm font-bold text-emerald-400">${model}</div>
        </div>
      </div>

      <div class="space-y-1">
        <span class="text-[11px] font-semibold text-slate-400 uppercase">Justificativa Arquitetural de Roteamento:</span>
        <p class="text-xs text-slate-300 leading-relaxed bg-slate-900/60 p-3 rounded-xl border border-slate-800">${rationale}</p>
      </div>

      ${top3.length ? `
        <div class="space-y-2 pt-2">
          <span class="text-[11px] font-semibold text-slate-400 uppercase">Top 3 Cascata Especulativa Recomendada:</span>
          <div class="flex flex-wrap gap-2">
            ${top3.map((m, idx) => `
              <span class="px-2.5 py-1 rounded-lg bg-slate-900 border border-slate-800 text-xs text-slate-200">
                <span class="text-cyan-400 font-bold">${idx + 1}.</span> ${m.name || m.model_id || m}
              </span>
            `).join("")}
          </div>
        </div>
      ` : ""}
    </div>
  `;
}
