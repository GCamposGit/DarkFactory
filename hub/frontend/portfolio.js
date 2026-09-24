/**
 * DarkHub - Multi-Project Portfolio Cockpit (DH-08)
 *
 * Provides read-only consolidated visibility into the development stage,
 * operational health, roadmap progression, deployment status, monthly budget,
 * worker slots, and archetypes across all projects adopted in Dark Factory.
 */

const portfolioState = {
  activeTab: "projects",
  data: null,
  efficiency: null,
  archetypes: null,
  selectedProject: null,
  loading: false,
  searchQuery: "",
};

function escapePortfolioHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function openPortfolioDrawer() {
  const drawer = document.getElementById("portfolio-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  loadPortfolioOverview();
}

function closePortfolioDrawer() {
  const drawer = document.getElementById("portfolio-drawer");
  if (!drawer) return;
  drawer.classList.add("hidden");
  document.body.style.overflow = "";
}

function switchPortfolioTab(tab) {
  portfolioState.activeTab = tab;
  const tabs = ["projects", "efficiency", "archetypes", "line"];
  tabs.forEach((t) => {
    const btn = document.getElementById(`portfolio-tab-${t}`);
    const view = document.getElementById(`portfolio-view-${t}`);
    const isActive = t === tab;
    if (btn) {
      btn.className = isActive
        ? "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 font-semibold"
        : "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all text-slate-400 hover:text-slate-200 border border-transparent";
      btn.setAttribute("aria-selected", String(isActive));
    }
    if (view) {
      view.classList.toggle("hidden", !isActive);
    }
  });

  if (tab === "efficiency" && !portfolioState.efficiency) {
    loadPortfolioEfficiency();
  } else if (tab === "archetypes" && !portfolioState.archetypes) {
    loadPortfolioArchetypes();
  }
}

async function loadPortfolioOverview() {
  portfolioState.loading = true;
  const grid = document.getElementById("portfolio-projects-grid");
  if (grid) {
    grid.innerHTML = `
      <div class="col-span-full py-16 text-center text-slate-500 text-xs font-mono">
        <span class="inline-block animate-spin mr-2">⚙️</span> Carregando portfólio multiprojeto...
      </div>
    `;
  }

  try {
    const res = await fetch("/api/portfolio");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    portfolioState.data = data;

    // Update KPI counters
    const totalEl = document.getElementById("portfolio-total-count");
    if (totalEl) totalEl.textContent = data.total_projects;

    const healthyEl = document.getElementById("portfolio-healthy-count");
    if (healthyEl) {
      healthyEl.textContent = `${data.healthy_projects} / ${data.total_projects}`;
      healthyEl.className = data.warning_projects === 0 ? "text-emerald-400 font-bold" : "text-amber-400 font-bold";
    }

    const budgetEl = document.getElementById("portfolio-budget-total");
    if (budgetEl) {
      budgetEl.textContent = `$${data.total_spent_usd.toFixed(2)} / $${data.total_budget_limit_usd.toFixed(2)}`;
    }

    const slotsEl = document.getElementById("portfolio-slots-count");
    if (slotsEl) {
      slotsEl.textContent = `H: ${data.active_heavy_slots}/${data.max_heavy_slots} · L: ${data.active_light_slots}/${data.max_light_slots}`;
    }

    renderPortfolioProjects();
    renderPortfolioLineTab();
  } catch (err) {
    if (grid) {
      grid.innerHTML = `
        <div class="col-span-full p-6 rounded-2xl bg-rose-950/20 border border-rose-500/30 text-rose-300 text-xs font-mono">
          Falha ao carregar visão geral do portfólio: ${escapePortfolioHtml(err.message)}
        </div>
      `;
    }
  } finally {
    portfolioState.loading = false;
  }
}

function renderPortfolioProjects() {
  const grid = document.getElementById("portfolio-projects-grid");
  if (!grid || !portfolioState.data) return;

  const query = portfolioState.searchQuery.toLowerCase().trim();
  const projects = portfolioState.data.projects.filter((p) => {
    if (!query) return true;
    return (
      p.id.toLowerCase().includes(query) ||
      p.name.toLowerCase().includes(query) ||
      p.description.toLowerCase().includes(query) ||
      p.kind.toLowerCase().includes(query)
    );
  });

  if (projects.length === 0) {
    grid.innerHTML = `
      <div class="col-span-full py-16 text-center space-y-2">
        <div class="text-3xl">🔍</div>
        <div class="text-slate-300 text-sm font-semibold">Nenhum projeto encontrado</div>
        <div class="text-slate-500 text-xs">Tente ajustar o termo da busca.</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = projects
    .map((p) => {
      const isHealthy = p.health_status === "healthy";
      const healthBadge = isHealthy
        ? `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-mono"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> Saudável</span>`
        : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 text-[10px] font-mono"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span> Atenção</span>`;

      const stageBadgeClass =
        p.dev_stage === "production"
          ? "bg-purple-500/10 text-purple-400 border-purple-500/20"
          : "bg-blue-500/10 text-blue-400 border-blue-500/20";

      const completionPct = p.roadmap_summary?.completion_pct || 0;
      const budgetPct = p.budget_summary?.utilization_pct || 0;
      const budgetStatusClass =
        p.budget_summary?.status === "local_only"
          ? "text-rose-400 font-bold"
          : budgetPct >= 80
          ? "text-amber-400 font-bold"
          : "text-slate-300";

      return `
        <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 hover:border-slate-700 transition-all flex flex-col justify-between gap-4 group">
          <div class="space-y-3">
            <div class="flex items-start justify-between gap-2">
              <div>
                <div class="flex items-center gap-2 flex-wrap">
                  <h3 class="text-sm font-bold text-white group-hover:text-indigo-300 transition-colors">${escapePortfolioHtml(p.name)}</h3>
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-300 border border-slate-700">${escapePortfolioHtml(p.prefix)}</span>
                </div>
                <p class="text-xs text-slate-400 mt-1 line-clamp-2">${escapePortfolioHtml(p.description || "Sem descrição informada.")}</p>
              </div>
              <div class="shrink-0 flex flex-col items-end gap-1.5">
                ${healthBadge}
                <span class="px-2 py-0.5 rounded-full border text-[10px] font-mono ${stageBadgeClass}">${escapePortfolioHtml(p.dev_stage)}</span>
              </div>
            </div>

            <!-- Key Indicators Grid -->
            <div class="grid grid-cols-2 gap-2 p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 text-xs font-mono">
              <div>
                <span class="text-[10px] text-slate-500 uppercase block">Roadmap</span>
                <div class="flex items-center gap-2 mt-1">
                  <div class="flex-1 h-1.5 rounded-full bg-slate-800 overflow-hidden">
                    <div class="h-full bg-indigo-500 rounded-full" style="width: ${completionPct}%"></div>
                  </div>
                  <span class="text-slate-300 text-[11px] font-semibold">${completionPct}%</span>
                </div>
                <span class="text-[9px] text-slate-500">${p.roadmap_summary?.delivered_items || 0}/${p.roadmap_summary?.total_items || 0} entregues</span>
              </div>

              <div>
                <span class="text-[10px] text-slate-500 uppercase block">Deploy & Smoke</span>
                <div class="mt-1 flex items-center gap-1.5 text-[11px]">
                  <span class="w-1.5 h-1.5 rounded-full ${p.last_deploy?.status === "deployed" ? "bg-emerald-400" : "bg-slate-500"}"></span>
                  <span class="text-slate-300 truncate" title="${escapePortfolioHtml(p.last_deploy?.target || "Nenhum")}">${escapePortfolioHtml(p.last_deploy?.target || "Nenhum")}</span>
                </div>
                <span class="text-[9px] text-slate-500">${p.last_deploy?.domain || p.default_branch || "local"}</span>
              </div>

              <div>
                <span class="text-[10px] text-slate-500 uppercase block">Orçamento USD</span>
                <span class="text-[11px] ${budgetStatusClass} block mt-0.5">$${(p.budget_summary?.current_spent_usd || 0).toFixed(2)} / $${(p.budget_summary?.monthly_limit_usd || 0).toFixed(2)}</span>
                <span class="text-[9px] text-slate-500">${budgetPct}% utilizado</span>
              </div>

              <div>
                <span class="text-[10px] text-slate-500 uppercase block">Adoção & Autonomia</span>
                <span class="text-[11px] text-slate-300 block mt-0.5">${p.adoption_summary?.is_adopted ? `Nível ${p.adoption_summary?.autonomy_level} · Lock OK` : "Não adotado"}</span>
                <span class="text-[9px] text-slate-500">${p.adoption_summary?.managed_files_checked || 0} arquivos gerenc.</span>
              </div>
            </div>
          </div>

          <div class="flex items-center justify-between pt-2 border-t border-slate-800/80">
            <span class="text-[10px] font-mono text-slate-500">ID: <span class="text-slate-400">${escapePortfolioHtml(p.id)}</span></span>
            <button
              type="button"
              onclick="inspectPortfolioProject('${escapePortfolioHtml(p.id)}')"
              class="px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 hover:border-indigo-500/50 text-xs font-mono transition-all flex items-center gap-1">
              <span>Inspecionar</span>
              <span>→</span>
            </button>
          </div>
        </div>
      `;
    })
    .join("");
}

async function inspectPortfolioProject(projectId) {
  const modal = document.getElementById("portfolio-detail-modal");
  const content = document.getElementById("portfolio-detail-content");
  if (!modal || !content) return;

  modal.classList.remove("hidden");
  content.innerHTML = `
    <div class="py-16 text-center text-slate-500 text-xs font-mono">
      <span class="inline-block animate-spin mr-2">⚙️</span> Carregando detalhes do projeto ${escapePortfolioHtml(projectId)}...
    </div>
  `;

  try {
    const res = await fetch(`/api/portfolio/projects/${encodeURIComponent(projectId)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const detail = await res.json();
    portfolioState.selectedProject = detail;

    const p = detail.project;
    const cmds = detail.commands || {};
    const smoke = detail.smoke_checks || [];
    const verif = detail.verification || {};
    const rHealth = detail.roadmap_health || {};

    content.innerHTML = `
      <div class="space-y-6">
        <!-- Top Info Header -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-5 rounded-2xl bg-slate-900 border border-slate-800">
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-base font-bold text-white">${escapePortfolioHtml(p.name)}</h3>
              <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">${escapePortfolioHtml(p.id)}</span>
              <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-300">${escapePortfolioHtml(p.kind)}</span>
            </div>
            <p class="text-xs text-slate-400 mt-1">${escapePortfolioHtml(p.description || "Sem descrição detalhada.")}</p>
            <div class="flex items-center gap-4 mt-2 text-[11px] font-mono text-slate-500">
              <span>Repo: <span class="text-slate-300">${escapePortfolioHtml(p.repo_url || "Local")}</span></span>
              <span>Branch: <span class="text-slate-300">${escapePortfolioHtml(p.default_branch)}</span></span>
              <span>Domínio: <span class="text-slate-300">${escapePortfolioHtml(p.domain || "Nenhum")}</span></span>
            </div>
          </div>
          <div class="flex flex-col sm:items-end gap-1.5 font-mono text-xs">
            <span class="text-slate-400">Estágio: <strong class="text-white">${escapePortfolioHtml(p.dev_stage)}</strong></span>
            <span class="text-slate-400">Saúde: <strong class="${p.health_status === "healthy" ? "text-emerald-400" : "text-amber-400"}">${escapePortfolioHtml(p.health_status)}</strong></span>
          </div>
        </div>

        <!-- 5 Dimensions Deep Dive -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <!-- Card 1: Comandos Resolvidos & Validação -->
          <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 space-y-3">
            <h4 class="text-xs font-mono font-bold uppercase tracking-wider text-indigo-400 flex items-center gap-1.5">
              <span>⚡</span> Comandos & Pipeline de Testes
            </h4>
            <div class="space-y-2 text-xs font-mono">
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Validação (Test Suite):</span>
                <code class="text-emerald-400 text-[11px]">${escapePortfolioHtml((cmds.validate && cmds.validate.join(" && ")) || "Autodetectado via detect.py")}</code>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Build:</span>
                <code class="text-slate-300 text-[11px]">${escapePortfolioHtml((cmds.build && cmds.build.join(" && ")) || "Nenhum comando explícito")}</code>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Setup:</span>
                <code class="text-slate-300 text-[11px]">${escapePortfolioHtml((cmds.setup && cmds.setup.join(" && ")) || "Nenhum comando explícito")}</code>
              </div>
            </div>
          </div>

          <!-- Card 2: Adoção & Integridade de Arquivos -->
          <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 space-y-3">
            <h4 class="text-xs font-mono font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-1.5">
              <span>🛡️</span> Adoção & Governança (.factory)
            </h4>
            <div class="space-y-2 text-xs font-mono">
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-slate-400">Lock de Proveniência:</span>
                <strong class="${verif.lock_valid ? "text-emerald-400" : "text-amber-400"}">${verif.lock_valid ? "Válido (Sem Drift)" : "Lock Ausente / Não Aplicável"}</strong>
              </div>
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-slate-400">Arquivos Gerenciados Checados:</span>
                <strong class="text-white">${verif.managed_files_checked || p.adoption_summary?.managed_files_checked || 0}</strong>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Problemas / Drift:</span>
                ${
                  verif.problems && verif.problems.length > 0
                    ? `<ul class="mt-1 space-y-1 text-rose-400 text-[11px] max-h-24 overflow-y-auto">${verif.problems.slice(0, 5).map((pr) => `<li>• ${escapePortfolioHtml(pr)}</li>`).join("")}</ul>`
                    : `<span class="text-emerald-400 text-[11px]">Nenhum problema detectado</span>`
                }
              </div>
            </div>
          </div>

          <!-- Card 3: Orçamento & Roteamento HF-23 -->
          <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 space-y-3">
            <h4 class="text-xs font-mono font-bold uppercase tracking-wider text-amber-400 flex items-center gap-1.5">
              <span>💰</span> Orçamento Mensal & Limites USD
            </h4>
            <div class="grid grid-cols-2 gap-2 text-xs font-mono">
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Teto Mensal:</span>
                <strong class="text-white text-sm">$${(p.budget_summary?.monthly_limit_usd || 0).toFixed(2)}</strong>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Gasto Atual:</span>
                <strong class="text-amber-400 text-sm">$${(p.budget_summary?.current_spent_usd || 0).toFixed(2)}</strong>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Restante:</span>
                <strong class="text-emerald-400 text-sm">$${(p.budget_summary?.remaining_usd || 0).toFixed(2)}</strong>
              </div>
              <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-[10px] text-slate-500 block uppercase">Status da Conta:</span>
                <strong class="text-white text-sm">${escapePortfolioHtml(p.budget_summary?.status || "active")}</strong>
              </div>
            </div>
          </div>

          <!-- Card 4: Roadmap & Saúde Operacional -->
          <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 space-y-3">
            <h4 class="text-xs font-mono font-bold uppercase tracking-wider text-emerald-400 flex items-center gap-1.5">
              <span>🗺️</span> Diagnóstico do Roadmap & Fontes
            </h4>
            <div class="space-y-2 text-xs font-mono">
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-slate-400">Total de Itens Compilados:</span>
                <strong class="text-white">${p.roadmap_summary?.total_items || 0}</strong>
              </div>
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-slate-400">Itens Entregues:</span>
                <strong class="text-emerald-400">${p.roadmap_summary?.delivered_items || 0} (${p.roadmap_summary?.completion_pct || 0}%)</strong>
              </div>
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
                <span class="text-slate-400">Status de Obsolescência (Stale):</span>
                <strong class="${rHealth.stale ? "text-amber-400" : "text-emerald-400"}">${rHealth.stale ? "Stale / Desatualizado" : "Atualizado (Fresh)"}</strong>
              </div>
            </div>
          </div>
        </div>

        <!-- Deploy & Smoke Checks Section -->
        <div class="p-5 rounded-2xl bg-slate-900/60 border border-slate-800 space-y-3">
          <h4 class="text-xs font-mono font-bold uppercase tracking-wider text-purple-400 flex items-center gap-1.5">
            <span>🚀</span> Verificações Pós-Deploy (Smoke Checks HTTP)
          </h4>
          ${
            smoke.length > 0
              ? `
              <div class="space-y-2">
                ${smoke
                  .map(
                    (s) => `
                  <div class="p-3 rounded-xl bg-slate-950 border border-slate-800/80 flex items-center justify-between font-mono text-xs">
                    <div class="flex items-center gap-2">
                      <span class="text-emerald-400">GET</span>
                      <a href="${escapePortfolioHtml(s.url)}" target="_blank" rel="noopener noreferrer" class="text-indigo-400 hover:underline">${escapePortfolioHtml(s.url)}</a>
                    </div>
                    <span class="text-slate-400">Status Esperado: <strong class="text-emerald-400">${s.expect_status || 200}</strong></span>
                  </div>
                `
                  )
                  .join("")}
              </div>
            `
              : `<div class="p-3 rounded-xl bg-slate-950 border border-slate-800/80 text-slate-500 font-mono text-xs">Nenhum smoke check configurado para este repositório.</div>`
          }
        </div>
      </div>
    `;
  } catch (err) {
    content.innerHTML = `
      <div class="p-6 rounded-2xl bg-rose-950/20 border border-rose-500/30 text-rose-300 text-xs font-mono">
        Falha ao inspecionar projeto: ${escapePortfolioHtml(err.message)}
      </div>
    `;
  }
}

function closePortfolioDetailModal() {
  const modal = document.getElementById("portfolio-detail-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  portfolioState.selectedProject = null;
}

async function loadPortfolioEfficiency() {
  const container = document.getElementById("portfolio-efficiency-content");
  if (!container) return;

  container.innerHTML = `
    <div class="py-16 text-center text-slate-500 text-xs font-mono">
      <span class="inline-block animate-spin mr-2">⚙️</span> Carregando capacidade e orçamentos...
    </div>
  `;

  try {
    const res = await fetch("/api/portfolio/efficiency");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const eff = await res.json();
    portfolioState.efficiency = eff;

    container.innerHTML = `
      <div class="space-y-6">
        <!-- Worker Pools Slots -->
        <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-4">
          <div class="flex items-center justify-between">
            <div>
              <h3 class="text-sm font-bold text-white flex items-center gap-2">
                <span>🖥️</span> Worker Execution Slots (HF-23)
              </h3>
              <p class="text-xs text-slate-400 mt-0.5">Slots de concorrência calibrados para ambiente Windows e GPU compartilhada.</p>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">1 Heavy / 4 Light</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div class="p-4 rounded-xl bg-slate-950 border border-slate-800 font-mono">
              <span class="text-[10px] uppercase text-slate-500 block">Heavy Slot (GPU / Builds / Holdout):</span>
              <div class="flex items-center gap-2 mt-1">
                <div class="w-3 h-3 rounded-full ${eff.active_heavy_slots > 0 ? "bg-amber-400 animate-pulse" : "bg-emerald-400"}"></div>
                <strong class="text-base text-white">${eff.active_heavy_slots} / ${eff.max_heavy_slots}</strong>
                <span class="text-xs text-slate-500">ativo(s)</span>
              </div>
            </div>

            <div class="p-4 rounded-xl bg-slate-950 border border-slate-800 font-mono">
              <span class="text-[10px] uppercase text-slate-500 block">Light Slots (IO / Subagentes / Research):</span>
              <div class="flex items-center gap-2 mt-1">
                <div class="w-3 h-3 rounded-full ${eff.active_light_slots > 0 ? "bg-indigo-400" : "bg-emerald-400"}"></div>
                <strong class="text-base text-white">${eff.active_light_slots} / ${eff.max_light_slots}</strong>
                <span class="text-xs text-slate-500">ativo(s)</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Budget Ceilings Grid -->
        <div class="space-y-3">
          <h3 class="text-xs font-mono font-bold uppercase tracking-wider text-slate-400 flex items-center gap-2">
            <span>💳</span> Tetos Orçamentários Mensais por Repositório
          </h3>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            ${(eff.budgets || [])
              .map((b) => {
                const pct = b.monthly_limit_usd > 0 ? ((b.current_spent_usd / b.monthly_limit_usd) * 100).toFixed(1) : 0;
                const statusColor = b.status === "local_only" ? "rose" : pct >= 80 ? "amber" : "emerald";
                return `
                  <div class="p-4 rounded-xl bg-slate-900 border border-slate-800 font-mono space-y-2">
                    <div class="flex items-center justify-between">
                      <strong class="text-xs text-white uppercase">${escapePortfolioHtml(b.project_id)}</strong>
                      <span class="px-2 py-0.5 rounded text-[10px] bg-${statusColor}-500/10 text-${statusColor}-400 border border-${statusColor}-500/20">${escapePortfolioHtml(b.status)}</span>
                    </div>
                    <div class="text-lg font-bold text-white">$${b.current_spent_usd.toFixed(2)} <span class="text-xs font-normal text-slate-500">/ $${b.monthly_limit_usd.toFixed(2)}</span></div>
                    <div class="w-full h-1.5 rounded-full bg-slate-800 overflow-hidden">
                      <div class="h-full bg-${statusColor}-500 rounded-full" style="width: ${Math.min(pct, 100)}%"></div>
                    </div>
                    <div class="flex items-center justify-between text-[10px] text-slate-500">
                      <span>${pct}% do teto</span>
                      <span>Resta: $${Math.max(0, b.monthly_limit_usd - b.current_spent_usd).toFixed(2)}</span>
                    </div>
                  </div>
                `;
              })
              .join("")}
          </div>
        </div>
      </div>
    `;
  } catch (err) {
    container.innerHTML = `
      <div class="p-6 rounded-2xl bg-rose-950/20 border border-rose-500/30 text-rose-300 text-xs font-mono">
        Falha ao carregar métricas de eficiência: ${escapePortfolioHtml(err.message)}
      </div>
    `;
  }
}

async function loadPortfolioArchetypes() {
  const container = document.getElementById("portfolio-archetypes-content");
  if (!container) return;

  container.innerHTML = `
    <div class="py-16 text-center text-slate-500 text-xs font-mono">
      <span class="inline-block animate-spin mr-2">⚙️</span> Carregando catálogo de arquétipos...
    </div>
  `;

  try {
    const res = await fetch("/api/portfolio/archetypes");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const archetypes = await res.json();
    portfolioState.archetypes = archetypes;

    container.innerHTML = `
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        ${archetypes
          .map(
            (a) => `
          <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 hover:border-indigo-500/40 transition-all flex flex-col justify-between space-y-4">
            <div class="space-y-3">
              <div class="flex items-start justify-between gap-2">
                <h3 class="text-sm font-bold text-white">${escapePortfolioHtml(a.title)}</h3>
                <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">${escapePortfolioHtml(a.id)}</span>
              </div>
              <p class="text-xs text-slate-400">${escapePortfolioHtml(a.description)}</p>
              
              <div class="p-3 rounded-xl bg-slate-950 border border-slate-800/80 font-mono text-xs space-y-1.5">
                <div class="text-[10px] text-slate-500 uppercase">Stack Tecnológico:</div>
                <div class="text-indigo-300">• Framework: <span class="text-white">${escapePortfolioHtml(a.stack?.framework || a.framework || "Astro / FastAPI")}</span></div>
                <div class="text-indigo-300">• Styling: <span class="text-white">${escapePortfolioHtml(a.stack?.styling || a.styling || "Tailwind CSS")}</span></div>
                <div class="text-indigo-300">• Runtime: <span class="text-white">${escapePortfolioHtml(a.stack?.runtime || a.runtime || "node / python")}</span></div>
              </div>
            </div>

            <div class="pt-3 border-t border-slate-800 text-[10px] font-mono text-slate-500 flex items-center justify-between">
              <span>Alvos de deploy:</span>
              <span class="text-slate-300 truncate max-w-[150px]">${escapePortfolioHtml((a.stack?.deployment_targets || a.deployment_targets || []).join(", ") || "dokploy, ftp")}</span>
            </div>
          </div>
        `
          )
          .join("")}
      </div>
    `;
  } catch (err) {
    container.innerHTML = `
      <div class="p-6 rounded-2xl bg-rose-950/20 border border-rose-500/30 text-rose-300 text-xs font-mono">
        Falha ao carregar arquétipos: ${escapePortfolioHtml(err.message)}
      </div>
    `;
  }
}

function renderPortfolioLineTab() {
  const container = document.getElementById("portfolio-line-content");
  if (!container || !portfolioState.data) return;

  const stages = [
    { id: "grill", name: "1. Grill (Intake & Desambiguação)", desc: "Entrevista determinística Gate G1 para eliminar lacunas materiais." },
    { id: "planning", name: "2. Planejamento (PRD & Handoffs)", desc: "Design técnico com contratos Pydantic v2 e handoffs pequenos." },
    { id: "build", name: "3. Build (Implementação & Testes)", desc: "Ciclo PIV com protocolo fail-closed e testes determinísticos." },
    { id: "review", name: "4. Review Adversarial Independente", desc: "Auditoria estrita de outra família de modelos sem acoplamento." },
    { id: "integration", name: "5. Integração & Holdout", desc: "Fusão determinística na branch principal e oráculos holdout." },
    { id: "release", name: "6. Release & Deploy", desc: "Entrega em produção Dokploy/Hostinger com verificação de smoke." },
  ];

  container.innerHTML = `
    <div class="space-y-6">
      <!-- Production Line Stages -->
      <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-4">
        <div class="flex items-center justify-between">
          <div>
            <h3 class="text-sm font-bold text-white flex items-center gap-2">
              <span>🏭</span> Esteira Autônoma de Produção (HF-27)
            </h3>
            <p class="text-xs text-slate-400 mt-0.5">Estágios padronizados para levar uma demanda do intake ao release em produção.</p>
          </div>
          <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">6 Estágios PIV</span>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          ${stages
            .map(
              (st) => `
            <div class="p-3.5 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
              <h4 class="text-xs font-mono font-bold text-white">${escapePortfolioHtml(st.name)}</h4>
              <p class="text-[11px] text-slate-400">${escapePortfolioHtml(st.desc)}</p>
            </div>
          `
            )
            .join("")}
        </div>
      </div>

      <!-- Pilots & Echo Garden Game Engine -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
        <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-3">
          <h3 class="text-xs font-mono font-bold uppercase tracking-wider text-amber-400 flex items-center gap-2">
            <span>🧪</span> Pilotos Estatísticos & Avaliações Shadow (HF-23-02)
          </h3>
          <p class="text-xs text-slate-400">Avaliações rigorosas de acurácia com intervalo de confiança de Wilson e teste de McNemar.</p>
          <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 font-mono text-xs space-y-1">
            <div class="flex justify-between text-slate-400"><span>Hipótese Principal:</span> <strong class="text-white">paired_stage_error_delta</strong></div>
            <div class="flex justify-between text-slate-400"><span>Tamanho Amostral Mínimo:</span> <strong class="text-white">10 demandas</strong></div>
            <div class="flex justify-between text-slate-400"><span>Veredito mais Recente:</span> <strong class="text-emerald-400">promising</strong></div>
          </div>
        </div>

        <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-3">
          <h3 class="text-xs font-mono font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
            <span>🎮</span> Domínio de Jogo Determinístico (Echo Garden)
          </h3>
          <p class="text-xs text-slate-400">Oráculo determinístico para benchmarking contínuo da fábrica autônoma e validação do motor semântico.</p>
          <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 font-mono text-xs space-y-1">
            <div class="flex justify-between text-slate-400"><span>Jogo:</span> <strong class="text-white">Echo Garden (Seed 0)</strong></div>
            <div class="flex justify-between text-slate-400"><span>Status:</span> <strong class="text-emerald-400">deterministic_ready</strong></div>
            <div class="flex justify-between text-slate-400"><span>Ações Válidas:</span> <strong class="text-indigo-300">weave, echo, ground</strong></div>
          </div>
        </div>
      </div>
    </div>
  `;
}

// Global hook for project search
function onPortfolioSearchChange(val) {
  portfolioState.searchQuery = val;
  renderPortfolioProjects();
}
