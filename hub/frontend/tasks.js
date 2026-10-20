/**
 * DarkHub - Status da linha e plano de autonomia (DF-21, USR-42, USR-215, USR-219).
 *
 * USR-219: a fila operacional e os cartoes WAITING_HUMAN sairam da pagina principal
 * (bloqueios acionaveis: fila prioritaria em interventions.js; execucoes: Esteira ao vivo).
 * Este arquivo mantem o status da linha (loadLineStatus) e o DAG da autonomia, somente leitura.
 */

const taskDashboardState = {
  loading: false,
  refreshPending: false,
  lineStatusLoading: false,
  lineStatusRefreshPending: false,
  pollTimer: null,
};

const TASK_DASHBOARD_POLL_MS = 30000;

function stopTaskDashboardPolling() {
  if (taskDashboardState.pollTimer !== null) {
    window.clearTimeout(taskDashboardState.pollTimer);
    taskDashboardState.pollTimer = null;
  }
}

function refreshVisibleTaskDashboard() {
  if (document.hidden) return;
  loadLineStatus();
  stopTaskDashboardPolling();
  taskDashboardState.pollTimer = window.setTimeout(() => {
    taskDashboardState.pollTimer = null;
    refreshVisibleTaskDashboard();
  }, TASK_DASHBOARD_POLL_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  mountAutonomyPlanToggle();
  refreshVisibleTaskDashboard();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopTaskDashboardPolling();
    else refreshVisibleTaskDashboard();
  });
});

// DAG da autonomia: recolhido por padrao; so busca o plano quando aberto (estado lembrado no navegador).
function mountAutonomyPlanToggle() {
  const toggle = document.getElementById("tasks-dag-toggle");
  const body = document.getElementById("tasks-dag-container");
  if (!toggle || !body) return;
  toggle.hubOnOpen = () => loadAutonomyPlan();
  const collapsed = typeof hubCollapseIsCollapsed === "function" ? hubCollapseIsCollapsed("tasks-dag") : true;
  body.classList.toggle("hidden", collapsed);
  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  toggle.textContent = collapsed ? "Abrir" : "Fechar";
  if (!collapsed) loadAutonomyPlan();
}

// USR-215: o que falta e o proximo passo de cada criterio, em linguagem simples (+ link da prova do V1).
function renderLineCriterionHint(statusEl, crit) {
  const id = `${statusEl.id}-hint`;
  let hint = document.getElementById(id);
  if (!hint) {
    hint = document.createElement("div");
    hint.id = id;
    hint.className = "mt-1 text-[10px] leading-snug text-slate-400";
    statusEl.insertAdjacentElement("afterend", hint);
  }
  hint.textContent = "";
  const details = crit.details || {};
  const lines = crit.verified ? [] : [details.missing, details.next_step].filter(Boolean);
  for (const text of lines) {
    const p = document.createElement("p");
    p.textContent = String(text);
    hint.appendChild(p);
  }
  const proofUrl = typeof details.proof_url === "string" && details.proof_url.startsWith("/live.html") ? details.proof_url : "";
  if (crit.verified && proofUrl) {
    const a = document.createElement("a");
    a.href = proofUrl;
    a.className = "text-indigo-300 hover:text-indigo-200 underline";
    a.textContent = `Ver a entrega na Esteira ao vivo (${details.proof_run_id || "run"})`;
    hint.appendChild(a);
  }
}

async function loadLineStatus() {
  const card = document.getElementById("line-status-card");
  if (!card) return;
  if (taskDashboardState.lineStatusLoading) {
    taskDashboardState.lineStatusRefreshPending = true;
    return;
  }
  taskDashboardState.lineStatusLoading = true;
  try {
    const request = typeof hubFetch === "function" ? hubFetch : fetch;
    const response = await request("/api/line/status", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) return;
    const data = await response.json();
    const streakBadge = document.getElementById("line-canary-streak-badge");
    if (streakBadge) {
      streakBadge.textContent = `Canário: ${data.canary_streak} dias verdes`;
    }
    if (data.acceptance && Array.isArray(data.acceptance.criteria)) {
      for (const crit of data.acceptance.criteria) {
        const idLower = String(crit.id || "").toLowerCase();
        const el = document.getElementById(`line-${idLower}-status`);
        if (el) {
          el.textContent = crit.verified ? "Aprovado" : "Pendente";
          el.className = crit.verified ? "font-medium text-emerald-400 mt-0.5" : "font-medium text-amber-400 mt-0.5";
          renderLineCriterionHint(el, crit);
        }
      }
    }
  } catch (_err) {
    // Gracefully ignore fetch errors
  } finally {
    taskDashboardState.lineStatusLoading = false;
    const refreshPending = taskDashboardState.lineStatusRefreshPending;
    taskDashboardState.lineStatusRefreshPending = false;
    if (refreshPending && !document.hidden) loadLineStatus();
  }
}

function taskSummaryCard(label, value, note, color) {
  return `<div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-3"><div class="text-[10px] uppercase tracking-wider text-slate-600">${tasksEscapeHtml(label)}</div><div class="mt-1 text-lg font-semibold ${color}">${tasksEscapeHtml(value)}</div><div class="mt-1 text-[10px] text-slate-500">${tasksEscapeHtml(note)}</div></div>`;
}

function tasksEscapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// =============================================================================
// DH-12: CONTINUOUS AUTONOMY PLAN & DAG VISUALIZATION
// =============================================================================

async function loadAutonomyPlan(force = false) {
  if (taskDashboardState.loadingPlan && !force) return;
  taskDashboardState.loadingPlan = true;
  try {
    const res = await hubNetFetch("/api/autonomy/plan", {
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    taskDashboardState.autonomyPlan = await res.json();
    renderAutonomyPlan(taskDashboardState.autonomyPlan);
  } catch (err) {
    console.warn("Falha ao carregar plano de autonomia:", err);
    const container = document.getElementById("autonomy-dag-nodes-grid");
    if (container) {
      container.innerHTML = `<div class="rounded-xl border border-rose-500/20 bg-rose-500/10 p-4 text-xs text-rose-300">Falha ao carregar o DAG da autonomia contínua: ${tasksEscapeHtml(err.message)}</div>`;
    }
  } finally {
    taskDashboardState.loadingPlan = false;
  }
}

function renderAutonomyPlan(plan) {
  if (!plan) return;
  const summaryEl = document.getElementById("autonomy-plan-summary");
  const gatesEl = document.getElementById("autonomy-gates-grid");
  const dagNodesEl = document.getElementById("autonomy-dag-nodes-grid");
  const badgeEl = document.getElementById("autonomy-dag-badge");

  if (badgeEl && plan.package_id) {
    badgeEl.textContent = `${plan.package_id} · ${plan.total_units} Unidades`;
  }

  // 1. Plan summary bar
  if (summaryEl) {
    const counts = plan.counts || {};
    summaryEl.innerHTML = [
      taskSummaryCard("Total Unidades", plan.total_units || 33, "Plano canônico de autonomia", "text-indigo-300"),
      taskSummaryCard("Concluídas", counts.completed || 0, "validadas no repositório", "text-emerald-300"),
      taskSummaryCard("Em Execução", counts.in_progress || 0, "slots ou workers", "text-cyan-300"),
      taskSummaryCard("Prontas p/ Handoff", counts.ready || 0, "contrato especificado", "text-amber-300"),
      taskSummaryCard("Baseline SHA", (plan.baseline_sha || "").slice(0, 7) || "83e5298", "imutável vinculada", "text-purple-300"),
    ].join("");
  }

  // 2. Readiness Gates
  if (gatesEl) {
    const gates = Array.isArray(plan.readiness_gates) ? plan.readiness_gates : [];
    gatesEl.innerHTML = gates.map((g) => `
      <div class="rounded-xl border border-slate-800 bg-slate-950/80 p-3 flex flex-col justify-between gap-2 shadow-sm">
        <div>
          <div class="flex items-center justify-between gap-1">
            <span class="font-mono text-[10px] text-slate-500">${tasksEscapeHtml(g.gate_id)}</span>
            <span class="inline-flex items-center gap-1 text-[10px] font-mono text-emerald-400">
              <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span> ${tasksEscapeHtml(g.status || "passed")}
            </span>
          </div>
          <h4 class="mt-1 text-xs font-semibold text-slate-200">${tasksEscapeHtml(g.name)}</h4>
          <p class="mt-0.5 text-[11px] text-slate-400 line-clamp-2">${tasksEscapeHtml(g.description)}</p>
        </div>
      </div>
    `).join("");
  }

  // 3. Units DAG Grouped by Waves
  if (dagNodesEl) {
    const units = Array.isArray(plan.units) ? plan.units : [];
    
    const waves = [
      { id: "wave-1", title: "Onda 1: Política Efetiva, Resolução & Baseline", filter: (u) => (u.ticket_id || "").startsWith("HF-26") },
      { id: "wave-2", title: "Onda 2: Intake Canônico & Demanda Durável", filter: (u) => (u.ticket_id || "").startsWith("HF-08") },
      { id: "wave-3", title: "Onda 3: Consumidores Permanentes & Executores", filter: (u) => (u.ticket_id || "").startsWith("HF-05") || (u.ticket_id || "").startsWith("HF-09") },
      { id: "wave-4", title: "Onda 4: Integração Remota, Deploy & Jornada", filter: (u) => (u.ticket_id || "").startsWith("HF-11") || (u.ticket_id || "").startsWith("HF-12") },
      { id: "wave-5", title: "Onda 5: Memória, Resiliência & Auto-Evolução", filter: (u) => ["HF-10", "HF-13", "HF-15", "HF-23", "HF-25", "HF-27"].some((prefix) => (u.ticket_id || "").startsWith(prefix)) },
      { id: "wave-other", title: "Outras Unidades da Fábrica", filter: (u) => !["HF-26", "HF-08", "HF-05", "HF-09", "HF-11", "HF-12", "HF-10", "HF-13", "HF-15", "HF-23", "HF-25", "HF-27"].some((p) => (u.ticket_id || "").startsWith(p)) },
    ];

    dagNodesEl.innerHTML = waves.map((wave) => {
      const waveUnits = units.filter(wave.filter);
      if (!waveUnits.length) return "";

      const cardsHtml = waveUnits.map((u) => {
        const statusColors = {
          completed: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
          succeeded: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
          running: "border-cyan-500/30 bg-cyan-500/10 text-cyan-300",
          ready_for_handoff: "border-indigo-500/30 bg-indigo-500/10 text-indigo-300",
          waiting_human: "border-amber-500/40 bg-amber-500/10 text-amber-300",
          not_started: "border-slate-800 bg-slate-950/60 text-slate-400",
        };
        const statusClass = statusColors[u.status] || statusColors.not_started;

        const depsHtml = (u.depends_on || []).map((dep) => `
          <span class="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono text-[9px] text-slate-400">${tasksEscapeHtml(dep)}</span>
        `).join("");

        const succsHtml = (u.successors || []).slice(0, 3).map((succ) => `
          <span class="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono text-[9px] text-slate-400">${tasksEscapeHtml(succ)}</span>
        `).join("");

        return `
          <div class="rounded-xl border border-slate-800/80 bg-slate-950/70 p-3.5 flex flex-col justify-between gap-3 hover:border-indigo-500/40 transition duration-150 shadow-sm" data-dag-unit="${tasksEscapeHtml(u.ticket_id)}">
            <div class="space-y-2">
              <div class="flex items-center justify-between gap-2">
                <span class="font-mono text-xs font-bold text-indigo-400">${tasksEscapeHtml(u.ticket_id)}</span>
                <span class="px-2 py-0.5 rounded-full text-[9px] font-mono border ${statusClass}">
                  ${tasksEscapeHtml(u.status)}
                </span>
              </div>
              <h4 class="text-xs font-semibold text-slate-200 line-clamp-2" title="${tasksEscapeHtml(u.title)}">${tasksEscapeHtml(u.title)}</h4>
              <div class="flex items-center gap-2 text-[10px] font-mono text-slate-500 flex-wrap">
                <span>Papel: <strong class="text-slate-300">${tasksEscapeHtml(u.executor_role)}</strong></span>
                <span>•</span>
                <span>Prioridade: <strong class="text-amber-400">${tasksEscapeHtml(u.priority)}</strong></span>
              </div>
              ${depsHtml ? `
                <div class="pt-1.5 border-t border-slate-900 flex items-center gap-1 flex-wrap">
                  <span class="text-[9px] text-slate-500 font-mono">Depende:</span>
                  ${depsHtml}
                </div>` : ""}
              ${succsHtml ? `
                <div class="flex items-center gap-1 flex-wrap">
                  <span class="text-[9px] text-slate-500 font-mono">Sucessores:</span>
                  ${succsHtml}
                </div>` : ""}
            </div>
            <div class="pt-2 border-t border-slate-900 flex items-center justify-between gap-2">
              <span class="text-[10px] font-mono text-slate-500">${u.live_stage ? `Etapa: ${tasksEscapeHtml(u.live_stage)}` : "DAG Nó"}</span>
              <button type="button" onclick="openAutonomyUnitModal('${tasksEscapeHtml(u.ticket_id)}')" class="px-2 py-1 rounded bg-slate-900 hover:bg-slate-800 text-[10px] font-mono text-indigo-300 border border-slate-800 transition active:scale-95">
                Inspecionar
              </button>
            </div>
          </div>`;
      }).join("");

      return `
        <div class="space-y-2">
          <h4 class="text-xs font-semibold text-slate-300 border-b border-slate-800 pb-1 flex items-center justify-between">
            <span>${tasksEscapeHtml(wave.title)}</span>
            <span class="text-[10px] font-mono text-slate-500">${waveUnits.length} unidade(s)</span>
          </h4>
          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            ${cardsHtml}
          </div>
        </div>`;
    }).join("");
  }
}

function ensureAutonomyUnitModalMounted() {
  if (document.getElementById("autonomy-unit-modal")) return;
  const modal = document.createElement("div");
  modal.id = "autonomy-unit-modal";
  modal.className = "hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4";
  modal.innerHTML = `
    <div class="w-full max-w-2xl rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 shadow-2xl max-h-[90vh] flex flex-col">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3 shrink-0">
        <h3 id="autonomy-unit-modal-title" class="text-sm font-semibold text-white flex items-center gap-2">
          <span>📄</span>
          <span>Detalhes do Contrato da Unidade</span>
        </h3>
        <button type="button" onclick="closeAutonomyUnitModal()" class="text-slate-400 hover:text-white transition">✕</button>
      </div>
      <div id="autonomy-unit-modal-body" class="space-y-3 text-xs text-slate-300 overflow-y-auto flex-1 pr-1"></div>
      <div class="flex items-center justify-end pt-3 border-t border-slate-800 shrink-0">
        <button type="button" onclick="closeAutonomyUnitModal()" class="px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition">Fechar</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

function closeAutonomyUnitModal() {
  const modal = document.getElementById("autonomy-unit-modal");
  if (modal) modal.classList.add("hidden");
}

function openAutonomyUnitModal(ticketId) {
  ensureAutonomyUnitModalMounted();
  const modal = document.getElementById("autonomy-unit-modal");
  const bodyEl = document.getElementById("autonomy-unit-modal-body");
  const titleEl = document.getElementById("autonomy-unit-modal-title");
  if (!modal || !bodyEl || !titleEl) return;

  const units = taskDashboardState.autonomyPlan?.units || [];
  const u = units.find((x) => x.ticket_id === ticketId);
  if (!u) {
    alert("Unidade não encontrada no plano.");
    return;
  }

  titleEl.innerHTML = `<span>📄</span><span>${tasksEscapeHtml(u.ticket_id)} — ${tasksEscapeHtml(u.title)}</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-3">
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] font-mono">
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800"><span class="text-slate-500 block text-[9px]">Status</span><span class="text-emerald-400 font-semibold">${tasksEscapeHtml(u.status)}</span></div>
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800"><span class="text-slate-500 block text-[9px]">Papel</span><span class="text-slate-200 font-semibold">${tasksEscapeHtml(u.executor_role)}</span></div>
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800"><span class="text-slate-500 block text-[9px]">Prioridade</span><span class="text-amber-300 font-semibold">${tasksEscapeHtml(u.priority)}</span></div>
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800"><span class="text-slate-500 block text-[9px]">Ambiente</span><span class="text-cyan-300 font-semibold">${tasksEscapeHtml(u.environment_profile)}</span></div>
      </div>
      
      ${u.oracle ? `
        <div class="p-3 rounded-xl bg-indigo-950/40 border border-indigo-800/40 space-y-1">
          <div class="text-[10px] uppercase font-mono text-indigo-300 font-bold">Oráculo Determinístico</div>
          <div class="text-slate-200">${tasksEscapeHtml(u.oracle)}</div>
        </div>` : ""}

      ${u.validate_cmd ? `
        <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
          <div class="text-[10px] uppercase font-mono text-slate-500 font-bold">Comando de Validação</div>
          <code class="text-emerald-400 font-mono text-xs block">${tasksEscapeHtml(u.validate_cmd)}</code>
        </div>` : ""}

      ${u.allowed_paths && u.allowed_paths.length ? `
        <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
          <div class="text-[10px] uppercase font-mono text-slate-500 font-bold">Caminhos Permitidos (Allowed Paths)</div>
          <div class="flex flex-wrap gap-1 pt-1">
            ${u.allowed_paths.map((p) => `<span class="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono text-[10px] text-slate-300">${tasksEscapeHtml(p)}</span>`).join("")}
          </div>
        </div>` : ""}

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
          <div class="text-[10px] uppercase font-mono text-slate-500 font-bold">Depende de</div>
          <div class="flex flex-wrap gap-1">
            ${(u.depends_on || []).length ? u.depends_on.map((d) => `<span class="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono text-[10px] text-indigo-300">${tasksEscapeHtml(d)}</span>`).join("") : '<span class="text-slate-600 text-[10px]">Nenhuma</span>'}
          </div>
        </div>
        <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
          <div class="text-[10px] uppercase font-mono text-slate-500 font-bold">Sucessores</div>
          <div class="flex flex-wrap gap-1">
            ${(u.successors || []).length ? u.successors.map((s) => `<span class="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono text-[10px] text-slate-300">${tasksEscapeHtml(s)}</span>`).join("") : '<span class="text-slate-600 text-[10px]">Nenhum</span>'}
          </div>
        </div>
      </div>
    </div>`;

  modal.classList.remove("hidden");
}
