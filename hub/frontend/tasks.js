/**
 * DarkHub - Operational task dashboard (DF-21).
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
    const response = await request("/api/tasks/dashboard", {
      headers: { Accept: "application/json" },
    });
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

  summary.innerHTML = [
    taskSummaryCard("Na fila", report.queued_count || queue.length, "tarefas visíveis", "text-indigo-300"),
    taskSummaryCard("Em execução", report.running_count || 0, "runs ativos", "text-emerald-300"),
    taskSummaryCard("Custo", formatTaskCost(report.total_cost_usd), "ledger de uso", "text-amber-300"),
    taskSummaryCard("Exceções", report.exception_count || 0, "sinais registrados", "text-rose-300"),
  ].join("");

  if (!queue.length) {
    queueContainer.innerHTML = '<div class="lg:col-span-2 rounded-2xl border border-dashed border-slate-800 bg-slate-900/40 p-8 text-center text-xs text-slate-500">Nenhuma tarefa registrada na fila operacional.</div>';
    return;
  }
  queueContainer.innerHTML = queue.map(renderTaskCard).join("");
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
  if (["RUNNING", "IMPLEMENTING", "VALIDATING"].includes(status)) return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  if (["MERGED", "READY_TO_MERGE"].includes(status)) return "border-cyan-500/30 bg-cyan-500/10 text-cyan-300";
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
