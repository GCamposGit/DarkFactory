/**
 * DarkHub - Operational task dashboard (DF-21, USR-42: canonical control store).
 *
 * This file only renders the read-only projection returned by the backend;
 * lifecycle mutations stay in the orchestrator and are never inferred here.
 */

const taskDashboardState = {
  loading: false,
  report: null,
};

document.addEventListener("DOMContentLoaded", () => {
  document
    .getElementById("tasks-dashboard-refresh")
    ?.addEventListener("click", () => loadTaskDashboard(true));
  loadTaskDashboard();
});

async function loadTaskDashboard(force = false) {
  if (taskDashboardState.loading && !force) return;
  taskDashboardState.loading = true;
  const alert = document.getElementById("tasks-dashboard-alert");
  if (alert) alert.textContent = "Atualizando fila operacional…";

  try {
    const request = typeof hubFetch === "function" ? hubFetch : fetch;
    let response = await request("/api/tasks/dashboard", {
      headers: { Accept: "application/json" },
    });
    if (response.status === 404) {
      response = await request("/tasks/dashboard", {
        headers: { Accept: "application/json" },
      });
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    taskDashboardState.report = await response.json();
    renderTaskDashboard();
  } catch (error) {
    taskDashboardState.report = null;
    renderTaskDashboardError(error);
  } finally {
    taskDashboardState.loading = false;
  }
}

function renderTaskDashboard() {
  const report = taskDashboardState.report || {};
  const queue = Array.isArray(report.queue) ? report.queue : [];
  const summary = document.getElementById("tasks-dashboard-summary");
  const alert = document.getElementById("tasks-dashboard-alert");
  const queueContainer = document.getElementById("tasks-dashboard-queue");
  if (!summary || !alert || !queueContainer) return;

  const warningCount = Array.isArray(report.warnings) ? report.warnings.length : 0;
  alert.className = warningCount
    ? "rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200"
    : "rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-200";
  alert.textContent = warningCount
    ? `Fila disponível com ${warningCount} aviso(s): ${report.warnings.join(" · ")}`
    : `Estado atualizado · fontes: ${formatTaskSources(report.sources)}`;

  const waitingHumanJobs = queue.filter(
    (item) => String(item.status || "").toUpperCase() === "WAITING_HUMAN"
  );
  const waitingHuman = waitingHumanJobs.length;
  summary.innerHTML = [
    taskSummaryCard("Na fila", report.queued_count || queue.length, waitingHuman ? `${waitingHuman} aguardando o owner` : "tarefas visíveis", "text-indigo-300"),
    taskSummaryCard("Em execução", report.running_count || 0, "runs ativos", "text-emerald-300"),
    taskSummaryCard("Custo", formatTaskCost(report.total_cost_usd), "ledger de uso", "text-amber-300"),
    taskSummaryCard("Exceções", report.exception_count || 0, "sinais registrados", "text-rose-300"),
  ].join("");

  if (!queue.length) {
    queueContainer.innerHTML = '<div class="lg:col-span-2 rounded-2xl border border-dashed border-slate-800 bg-slate-900/40 p-8 text-center text-xs text-slate-500">Nenhuma tarefa registrada na fila operacional.</div>';
    return;
  }
  const observationCards = waitingHumanJobs.map(renderWaitingHumanObservationCard).join("");
  const regularCards = queue.map(renderTaskCard).join("");
  queueContainer.innerHTML = observationCards + regularCards;
}

function getSuspensionReason(item) {
  if (item.diagnostic) return item.diagnostic;
  if (item.cause_code) return item.cause_code;
  if (Array.isArray(item.evidence)) {
    const diag = item.evidence.find((e) => e && e.label === "diagnóstico");
    if (diag && diag.value) return diag.value;
  }
  if (Array.isArray(item.exceptions) && item.exceptions.length > 0) {
    return item.exceptions[0];
  }
  return item.stage ? `Suspenso na etapa ${item.stage}` : "Alinhamento Grill pendente com o Owner";
}

const CANONICAL_GRILL_INSTRUCTION = "Resolução pelo terminal canônico: python -m core.demands.cli grill <ticket_id>";

function renderWaitingHumanObservationCard(item) {
  const reason = getSuspensionReason(item);
  const ticketId = tasksEscapeHtml(item.task_id || "<ticket_id>");
  const runId = tasksEscapeHtml(item.run_id || "sem run");
  const title = tasksEscapeHtml(item.title || item.task_id || "Demanda sem título");

  return `<section class="lg:col-span-2 rounded-2xl border-2 border-amber-500/50 bg-gradient-to-r from-amber-950/40 via-slate-900/90 to-amber-950/30 p-5 shadow-xl shadow-amber-950/20" data-waiting-human-card="${ticketId}">
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/20 pb-3">
      <div class="flex items-center gap-2">
        <span class="flex h-3 w-3 relative">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-3 w-3 bg-amber-500"></span>
        </span>
        <h3 class="text-xs font-bold uppercase tracking-wider text-amber-300">Aguardando Decisão do Owner (WAITING_HUMAN)</h3>
      </div>
      <span class="font-mono text-[10px] text-amber-400/90 bg-amber-500/10 border border-amber-500/30 px-2.5 py-0.5 rounded-full font-medium">Somente Leitura</span>
    </div>
    <div class="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
      <div>
        <div class="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Demanda / Título</div>
        <div class="mt-1 text-sm font-semibold text-slate-100">${title}</div>
        <div class="mt-0.5 text-[10px] font-mono text-slate-400">ID: ${ticketId}</div>
      </div>
      <div>
        <div class="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Run ID</div>
        <div class="mt-1 font-mono text-xs text-slate-200">${runId}</div>
      </div>
    </div>
    <div class="mt-3 rounded-lg bg-amber-500/10 border border-amber-500/20 p-3">
      <div class="text-[10px] uppercase tracking-wider font-semibold text-amber-300">Motivo da Suspensão</div>
      <div class="mt-1 text-xs text-amber-200 font-medium">${tasksEscapeHtml(reason)}</div>
    </div>
    <div class="mt-3 rounded-lg bg-slate-950/80 border border-slate-800 p-3">
      <div class="text-[10px] uppercase tracking-wider font-semibold text-slate-400">Instrução de Resolução</div>
      <div class="mt-1 text-xs font-mono text-emerald-400 select-all" data-instruction="${CANONICAL_GRILL_INSTRUCTION}">Resolução pelo terminal canônico: python -m core.demands.cli grill ${ticketId}</div>
      <div class="mt-1 text-[10px] text-slate-500 font-mono">Padrão canônico: Resolução pelo terminal canônico: python -m core.demands.cli grill &lt;ticket_id&gt;</div>
    </div>
  </section>`;
}

function renderTaskCard(item) {
  const exceptions = Array.isArray(item.exceptions) ? item.exceptions : [];
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  const status = String(item.status || "UNSET");
  const statusStyle = taskStatusStyle(status);
  const evidenceMarkup = evidence.length
    ? `<ul class="mt-2 space-y-1">${evidence.map((entry) => `<li class="text-[10px] text-slate-400"><span class="text-slate-300">${tasksEscapeHtml(entry.label)}</span><span class="text-slate-600"> · </span>${tasksEscapeHtml(entry.value)}${entry.source ? ` <span class="text-slate-600">(${tasksEscapeHtml(entry.source)})</span>` : ""}</li>`).join("")}</ul>`
    : '<p class="mt-2 text-[10px] text-slate-600">Nenhuma evidência vinculada.</p>';
  const exceptionMarkup = exceptions.length
    ? `<div class="mt-3 rounded-lg border border-rose-500/20 bg-rose-500/10 p-2"><div class="text-[10px] font-semibold uppercase tracking-wider text-rose-300">Exceções</div><ul class="mt-1 space-y-1">${exceptions.map((entry) => `<li class="text-[10px] text-rose-200">${tasksEscapeHtml(entry)}</li>`).join("")}</ul></div>`
    : "";

  return `<article class="rounded-2xl border border-slate-800 bg-slate-900/70 p-4 shadow-lg shadow-black/10" data-task-id="${tasksEscapeHtml(item.task_id)}">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0"><div class="text-[10px] font-mono text-slate-600">#${tasksEscapeHtml(item.queue_position)} · ${tasksEscapeHtml(item.task_id)}</div><h3 class="mt-1 truncate text-sm font-semibold text-slate-100">${tasksEscapeHtml(item.title)}</h3></div>
      <span class="shrink-0 rounded-full border px-2 py-1 text-[10px] font-mono ${statusStyle}">${tasksEscapeHtml(status)}</span>
    </div>
    <div class="mt-3 grid grid-cols-2 gap-2 text-[10px]">
      <div class="rounded-lg bg-slate-950/70 p-2"><div class="text-slate-600">Etapa</div><div class="mt-1 text-slate-300">${tasksEscapeHtml(item.stage || "—")}</div></div>
      <div class="rounded-lg bg-slate-950/70 p-2"><div class="text-slate-600">Run</div><div class="mt-1 truncate font-mono text-slate-300">${tasksEscapeHtml(item.run_id || "sem run")}</div></div>
      <div class="rounded-lg bg-slate-950/70 p-2"><div class="text-slate-600">Passo</div><div class="mt-1 text-slate-300">${tasksEscapeHtml(item.step_index ?? "—")}</div></div>
      <div class="rounded-lg bg-slate-950/70 p-2"><div class="text-slate-600">Custo da tarefa</div><div class="mt-1 text-amber-300">${formatTaskCost(item.cost_usd)}</div></div>
    </div>
    <div class="mt-3 border-t border-slate-800/80 pt-3"><div class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Evidências</div>${evidenceMarkup}</div>
    ${exceptionMarkup}
  </article>`;
}

function taskSummaryCard(label, value, note, color) {
  return `<div class="rounded-2xl border border-slate-800 bg-slate-900/65 p-3"><div class="text-[10px] uppercase tracking-wider text-slate-600">${tasksEscapeHtml(label)}</div><div class="mt-1 text-lg font-semibold ${color}">${tasksEscapeHtml(value)}</div><div class="mt-1 text-[10px] text-slate-500">${tasksEscapeHtml(note)}</div></div>`;
}

function taskStatusStyle(status) {
  if (["FAILED", "NEEDS_FIX"].includes(status)) return "border-rose-500/30 bg-rose-500/10 text-rose-300";
  if (["WAITING_HUMAN", "RETRY", "REPLAN", "WAITING_DEPENDENCY"].includes(status)) return "border-amber-500/30 bg-amber-500/10 text-amber-300";
  if (["RUNNING", "IMPLEMENTING", "VALIDATING"].includes(status)) return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  if (["MERGED", "READY_TO_MERGE", "SUCCEEDED"].includes(status)) return "border-cyan-500/30 bg-cyan-500/10 text-cyan-300";
  return "border-slate-700 bg-slate-800/70 text-slate-300";
}

function formatTaskCost(value) {
  const amount = Number(value || 0);
  if (!Number.isFinite(amount)) return "$0.00";
  return `$${amount.toFixed(4)}`;
}

function formatTaskSources(sources) {
  if (!sources || typeof sources !== "object") return "indisponíveis";
  return Object.entries(sources).map(([key, value]) => `${key}:${value}`).join(" · ") || "indisponíveis";
}

function renderTaskDashboardError(error) {
  const alert = document.getElementById("tasks-dashboard-alert");
  const summary = document.getElementById("tasks-dashboard-summary");
  const queue = document.getElementById("tasks-dashboard-queue");
  if (alert) {
    alert.className = "rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-200";
    alert.textContent = `Fila indisponível: ${error?.message || "erro desconhecido"}`;
  }
  if (summary) summary.innerHTML = "";
  if (queue) queue.innerHTML = '<div class="lg:col-span-2 rounded-2xl border border-dashed border-rose-500/20 bg-rose-500/5 p-8 text-center text-xs text-rose-300">Recarregue para tentar novamente.</div>';
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

function switchTaskViewMode(mode) {
  taskDashboardState.viewMode = mode;
  const btnQueue = document.getElementById("tasks-view-btn-queue");
  const btnDag = document.getElementById("tasks-view-btn-dag");
  const queueContainer = document.getElementById("tasks-queue-container");
  const dagContainer = document.getElementById("tasks-dag-container");
  const alert = document.getElementById("tasks-dashboard-alert");

  if (mode === "dag") {
    if (btnDag) btnDag.className = "px-2.5 py-1 rounded-lg bg-indigo-600 text-white font-mono text-[11px] shadow-sm transition";
    if (btnQueue) btnQueue.className = "px-2.5 py-1 rounded-lg text-slate-400 hover:text-slate-200 font-mono text-[11px] transition";
    if (queueContainer) queueContainer.classList.add("hidden");
    if (dagContainer) dagContainer.classList.remove("hidden");
    if (alert) alert.textContent = "Visualizando DAG causal e gates da autonomia contínua (HF-26 / HF-27)";
    loadAutonomyPlan();
  } else {
    if (btnQueue) btnQueue.className = "px-2.5 py-1 rounded-lg bg-indigo-600 text-white font-mono text-[11px] shadow-sm transition";
    if (btnDag) btnDag.className = "px-2.5 py-1 rounded-lg text-slate-400 hover:text-slate-200 font-mono text-[11px] transition";
    if (dagContainer) dagContainer.classList.add("hidden");
    if (queueContainer) queueContainer.classList.remove("hidden");
    if (taskDashboardState.report) {
      renderTaskDashboard();
    } else {
      loadTaskDashboard();
    }
  }
}

async function loadAutonomyPlan(force = false) {
  if (taskDashboardState.loadingPlan && !force) return;
  taskDashboardState.loadingPlan = true;
  try {
    const res = await fetch("/api/autonomy/plan", {
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
      taskSummaryCard("Total Unidades", plan.total_units || 33, "DAG canônico HF-26", "text-indigo-300"),
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

