/**
 * DarkHub - User Demands & Backlog Controller.
 *
 * Provides headless & UI-assisted input for manual user demands.
 * Operates at zero cost ($0.00) using local Ollama or deterministic script fallback.
 * Integrates directly with the Operational Roadmap tab for status tracking.
 */

const demandsUi = {
  projectId: "darkfac",
  loading: false,
  guidance: null,
  recentTickets: [],
};

document.addEventListener("DOMContentLoaded", () => {
  const drawer = document.getElementById("demands-drawer");
  if (drawer) {
    drawer.addEventListener("click", (event) => {
      if (event.target === drawer) closeDemandsDrawer();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("demands-drawer")?.classList.contains("hidden")) {
      closeDemandsDrawer();
    }
  });
});

function openDemandsDrawer() {
  const drawer = document.getElementById("demands-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  document.body.classList.add("overflow-hidden");
  loadDemandsProjects();
  loadRecentDemands();
  setTimeout(() => document.getElementById("demand-title-input")?.focus(), 50);
}

function closeDemandsDrawer() {
  const drawer = document.getElementById("demands-drawer");
  if (drawer) drawer.classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
}

async function loadDemandsProjects() {
  try {
    const response = await fetch("/api/projects");
    if (!response.ok) return;
    const projects = await response.json();
    const select = document.getElementById("demand-project-select");
    if (select && projects.length) {
      select.innerHTML = projects
        .map((p) => `<option value="${escapeDemandsHtml(p.id)}">${escapeDemandsHtml(p.name)} (${escapeDemandsHtml(p.id)})</option>`)
        .join("");
      select.value = demandsUi.projectId;
      select.onchange = () => {
        demandsUi.projectId = select.value;
        const globalSel = document.getElementById("global-project-select");
        if (globalSel && globalSel.value !== select.value) {
          globalSel.value = select.value;
          localStorage.setItem("darkhub_active_project", select.value);
          window.currentActiveProjectId = select.value;
          if (typeof syncActiveProjectToComponents === "function") {
            syncActiveProjectToComponents(select.value);
          }
        }
        loadRecentDemands();
      };
    }
  } catch (err) {
    console.warn("Could not load projects for demands:", err);
  }
}

async function runDemandGuidance(forceHeuristic = false) {
  const titleInput = document.getElementById("demand-title-input");
  const problemInput = document.getElementById("demand-problem-input");
  const journeyInput = document.getElementById("demand-journey-input");
  const nongoalsInput = document.getElementById("demand-nongoals-input");
  const criteriaInput = document.getElementById("demand-criteria-input");
  const horizonSelect = document.getElementById("demand-horizon-select");
  const typeSelect = document.getElementById("demand-type-select");
  const projectSelect = document.getElementById("demand-project-select");

  const title = titleInput?.value.trim() || "";
  if (title.length < 3) {
    showToast("Informe pelo menos um título básico para orientar a demanda.", "warning");
    titleInput?.focus();
    return;
  }

  const payload = {
    project_id: projectSelect?.value || "darkfac",
    title: title,
    problem_statement: problemInput?.value.trim() || "",
    core_journey: journeyInput?.value.trim() || "",
    non_goals: (nongoalsInput?.value || "").split("\n").map((s) => s.trim()).filter(Boolean),
    acceptance_criteria: (criteriaInput?.value || "").split("\n").map((s) => s.trim()).filter(Boolean),
    horizon: horizonSelect?.value || "now",
    item_type: typeSelect?.value || "feature",
  };

  const btn = document.getElementById("btn-guide-demand");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin inline-block mr-1">⏳</span> Analisando localmente ($0)...';
  }

  try {
    const query = forceHeuristic ? "?force_heuristic=true" : "";
    const res = await fetch(`/api/demands/guide${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: jsonStringify(payload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Falha na análise de demanda");
    }

    const guidance = await res.json();
    demandsUi.guidance = guidance;
    renderDemandGuidance(guidance);
    showToast("Orientação gerada com sucesso via motor local ($0.00)!", "success");
  } catch (error) {
    console.error("Guidance error:", error);
    showToast(`Erro na orientação: ${error.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span>🧠</span> Orientar & Especificar ($0.00)';
    }
  }
}

function renderDemandGuidance(guidance) {
  const container = document.getElementById("demands-guidance-container");
  if (!container) return;

  container.classList.remove("hidden");

  // Readiness gauge
  const score = guidance.readiness_score || 0;
  const isReady = guidance.is_ready;
  const scoreColor = score >= 75 ? "text-emerald-400" : score >= 50 ? "text-amber-400" : "text-rose-400";
  const barColor = score >= 75 ? "bg-emerald-500" : score >= 50 ? "bg-amber-500" : "bg-rose-500";

  let missingHtml = "";
  if (guidance.missing_elements?.length) {
    missingHtml = `
      <div class="p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-amber-300 space-y-1">
        <strong class="font-semibold block flex items-center gap-1.5">
          <span>⚠️</span> Lacunas para fechar antes da execução:
        </strong>
        <ul class="list-disc list-inside space-y-0.5 text-[11px] text-amber-200/80">
          ${guidance.missing_elements.map((m) => `<li>${escapeDemandsHtml(m)}</li>`).join("")}
        </ul>
      </div>
    `;
  }

  let suggestionsHtml = "";
  if (guidance.suggestions?.length) {
    suggestionsHtml = `
      <div class="p-3 rounded-xl bg-slate-900 border border-slate-800 text-xs text-slate-300 space-y-1">
        <strong class="font-semibold text-slate-200 block flex items-center gap-1.5">
          <span>💡</span> Recomendações do Arquiteto:
        </strong>
        <ul class="list-disc list-inside space-y-0.5 text-[11px] text-slate-400">
          ${guidance.suggestions.map((s) => `<li>${escapeDemandsHtml(s)}</li>`).join("")}
        </ul>
      </div>
    `;
  }

  const ticket = guidance.suggested_ticket;
  let ticketPreview = "";
  if (ticket) {
    ticketPreview = `
      <div class="p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-3 font-mono text-xs">
        <div class="flex items-center justify-between">
          <span class="text-indigo-400 font-bold">${escapeDemandsHtml(ticket.id)}</span>
          <span class="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 text-[10px]">
            user-demand
          </span>
        </div>
        <div>
          <span class="text-slate-500 text-[10px] uppercase block">Título Refinado</span>
          <span class="text-slate-200 font-sans font-medium">${escapeDemandsHtml(ticket.title)}</span>
        </div>
        ${ticket.non_goals?.length ? `
          <div>
            <span class="text-slate-500 text-[10px] uppercase block">Non-Goals Propostos</span>
            <ul class="text-[11px] text-slate-400 list-disc list-inside font-sans">
              ${ticket.non_goals.map((ng) => `<li>${escapeDemandsHtml(ng)}</li>`).join("")}
            </ul>
          </div>
        ` : ""}
        ${ticket.acceptance_criteria?.length ? `
          <div>
            <span class="text-slate-500 text-[10px] uppercase block">Critérios de Aceitação</span>
            <ul class="text-[11px] text-slate-400 list-disc list-inside font-sans">
              ${ticket.acceptance_criteria.map((ac) => `<li>${escapeDemandsHtml(ac)}</li>`).join("")}
            </ul>
          </div>
        ` : ""}
        ${ticket.reachability_contract ? `
          <div>
            <span class="text-slate-500 text-[10px] uppercase block">Reachability (Headless CLI/HTTP)</span>
            <code class="text-[11px] text-cyan-300 block bg-slate-900 px-2 py-1 rounded border border-slate-800">
              ${escapeDemandsHtml(ticket.reachability_contract)}
            </code>
          </div>
        ` : ""}
        <button
          type="button"
          onclick="applySuggestedDemandSpec()"
          class="w-full py-2 rounded-xl bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 text-xs font-sans font-medium transition-all flex items-center justify-center gap-1.5">
          <span>✨</span> Aplicar Sugestões ao Formulário
        </button>
      </div>
    `;
  }

  container.innerHTML = `
    <div class="space-y-4">
      <div class="flex items-center justify-between pb-3 border-b border-slate-800">
        <div>
          <span class="text-[10px] font-mono uppercase tracking-wider text-slate-500">Prontidão da Demanda</span>
          <div class="flex items-baseline gap-2 mt-0.5">
            <span class="text-2xl font-bold font-mono ${scoreColor}">${score}%</span>
            <span class="text-xs text-slate-400 font-sans">${isReady ? "Pronta para Backlog" : "Necessita Refinamento"}</span>
          </div>
        </div>
        <div class="text-right font-mono text-[10px] text-slate-500">
          <div>Motor: <span class="text-slate-300">${escapeDemandsHtml(guidance.engine_used)}</span></div>
          <div>Custo: <span class="text-emerald-400">$0.00 (Grátis)</span></div>
        </div>
      </div>
      <div class="w-full bg-slate-800 h-2 rounded-full overflow-hidden">
        <div class="${barColor} h-full transition-all duration-500" style="width: ${score}%"></div>
      </div>
      ${missingHtml}
      ${suggestionsHtml}
      ${ticketPreview}
    </div>
  `;
}

function applySuggestedDemandSpec() {
  if (!demandsUi.guidance?.suggested_ticket) return;
  const t = demandsUi.guidance.suggested_ticket;

  const titleInput = document.getElementById("demand-title-input");
  const problemInput = document.getElementById("demand-problem-input");
  const journeyInput = document.getElementById("demand-journey-input");
  const nongoalsInput = document.getElementById("demand-nongoals-input");
  const criteriaInput = document.getElementById("demand-criteria-input");

  if (t.title && titleInput) titleInput.value = t.title;
  if (t.problem_statement && problemInput) problemInput.value = t.problem_statement;
  if (t.core_journey?.length && journeyInput) journeyInput.value = t.core_journey.join("\n");
  if (t.non_goals?.length && nongoalsInput) nongoalsInput.value = t.non_goals.join("\n");
  if (t.acceptance_criteria?.length && criteriaInput) criteriaInput.value = t.acceptance_criteria.join("\n");

  showToast("Sugestões aplicadas aos campos do formulário!", "success");
}

async function saveDemandToBacklog() {
  const titleInput = document.getElementById("demand-title-input");
  const problemInput = document.getElementById("demand-problem-input");
  const journeyInput = document.getElementById("demand-journey-input");
  const nongoalsInput = document.getElementById("demand-nongoals-input");
  const criteriaInput = document.getElementById("demand-criteria-input");
  const horizonSelect = document.getElementById("demand-horizon-select");
  const typeSelect = document.getElementById("demand-type-select");
  const projectSelect = document.getElementById("demand-project-select");

  const title = titleInput?.value.trim() || "";
  if (title.length < 3) {
    showToast("Preencha o título da demanda antes de salvar.", "warning");
    titleInput?.focus();
    return;
  }

  // If guidance was not run or user modified, compose ticket
  let ticketId = demandsUi.guidance?.suggested_ticket?.id;
  if (!ticketId) {
    try {
      const idRes = await fetch(`/api/demands/next-id?project_id=${encodeURIComponent(projectSelect?.value || "darkfac")}`);
      if (idRes.ok) {
        const idData = await idRes.json();
        ticketId = idData.next_id;
      }
    } catch (e) {
      ticketId = "AUTO";
    }
  }
  if (!ticketId) ticketId = "AUTO";

  const nonGoals = (nongoalsInput?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const criteria = (criteriaInput?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
  const journey = (journeyInput?.value || "").split("\n").map((s) => s.trim()).filter(Boolean);

  const ticketPayload = {
    id: ticketId,
    project_id: projectSelect?.value || "darkfac",
    title: title,
    origin: "user",
    status: "planned",
    item_type: typeSelect?.value || "feature",
    lifecycle_stage: "execution",
    horizon: horizonSelect?.value || "now",
    tags: ["user-demand", typeSelect?.value || "feature"],
    problem_statement: problemInput?.value.trim() || "",
    core_journey: journey,
    non_goals: nonGoals.length ? nonGoals : ["Não modificar arquivos fora do escopo desta demanda"],
    reachability_contract: `python -m pytest tests/test_${title.toLowerCase().replace(/[^a-z0-9]+/g, "_").slice(0, 25)}.py -v`,
    acceptance_criteria: criteria.length ? criteria : [`A funcionalidade descrita em '${title}' é validada sem regressões`],
    suggested_files: [],
    estimated_complexity: "medium",
    dependencies: [],
  };

  const btn = document.getElementById("btn-save-demand");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin inline-block mr-1">⏳</span> Salvando...';
  }

  try {
    const res = await fetch("/api/demands/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: jsonStringify(ticketPayload),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Erro ao salvar ticket no backlog");
    }

    const saved = await res.json();
    showToast(`Ticket ${saved.id} incluído com sucesso no Backlog da Dark Factory!`, "success");

    // Clear guidance & reload
    demandsUi.guidance = null;
    document.getElementById("demands-guidance-container")?.classList.add("hidden");
    resetDemandForm();
    loadRecentDemands();

    // Show completion alert with direct button to track in Roadmap
    renderSavedSuccessCard(saved);
  } catch (error) {
    console.error("Save demand error:", error);
    showToast(`Falha ao salvar demanda: ${error.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span>💾</span> Salvar no Backlog de Desenvolvimento';
    }
  }
}

function renderSavedSuccessCard(ticket) {
  const container = document.getElementById("demands-guidance-container");
  if (!container) return;
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="p-5 rounded-2xl bg-emerald-950/40 border border-emerald-500/30 text-emerald-300 space-y-4 animate-in-quick">
      <div class="flex items-start gap-3">
        <span class="text-2xl">🎉</span>
        <div>
          <h4 class="font-bold text-white text-sm">Demanda Incluída no Backlog!</h4>
          <p class="text-xs text-emerald-300/80 mt-1 font-sans">
            O ticket <strong class="font-mono text-emerald-200">${escapeDemandsHtml(ticket.id)}</strong> foi persistido com a tag 
            <code class="px-1.5 py-0.5 rounded bg-emerald-900/60 font-mono text-[11px]">user-demand</code> e já está disponível para o ciclo de desenvolvimento da fábrica.
          </p>
        </div>
      </div>
      <div class="pt-2 border-t border-emerald-500/20 flex flex-col sm:flex-row items-center gap-3">
        <button
          type="button"
          onclick="trackDemandInRoadmap('${escapeDemandsHtml(ticket.id)}')"
          class="w-full sm:w-auto px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-medium text-xs shadow-lg shadow-emerald-600/30 transition-all flex items-center justify-center gap-1.5">
          <span>🗺️</span> Acompanhar no Roadmap Operacional
        </button>
        <button
          type="button"
          onclick="document.getElementById('demands-guidance-container').classList.add('hidden')"
          class="text-xs text-emerald-400 hover:text-white font-mono">
          Abrir outra demanda
        </button>
      </div>
    </div>
  `;
}

function trackDemandInRoadmap(ticketId = "") {
  closeDemandsDrawer();
  if (typeof openRoadmapDrawer === "function") {
    openRoadmapDrawer();
    // If ticketId provided, fill search input and trigger reload
    setTimeout(() => {
      const search = document.getElementById("roadmap-search-input");
      if (search) {
        search.value = ticketId || "user-demand";
        if (typeof loadRoadmapSnapshot === "function") {
          loadRoadmapSnapshot();
        }
      }
    }, 120);
  }
}

async function loadRecentDemands() {
  const container = document.getElementById("recent-demands-list");
  if (!container) return;

  try {
    const res = await fetch(`/api/demands/tickets?project_id=${encodeURIComponent(demandsUi.projectId)}`);
    if (!res.ok) return;
    const tickets = await res.json();
    demandsUi.recentTickets = tickets;

    if (!tickets.length) {
      container.innerHTML = '<p class="text-[11px] text-slate-500 font-mono p-3 text-center">Nenhuma demanda manual aberta ainda.</p>';
      return;
    }

    container.innerHTML = tickets.map((t) => `
      <div class="p-3 rounded-xl bg-slate-900/70 border border-slate-800 hover:border-slate-700 transition-colors flex items-center justify-between gap-3">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="font-mono text-xs font-bold text-indigo-400">${escapeDemandsHtml(t.id)}</span>
            <span class="px-2 py-0.2 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-mono">user-demand</span>
            <span class="text-[10px] text-slate-500 font-mono">(${escapeDemandsHtml(t.status)})</span>
          </div>
          <p class="text-xs text-slate-200 truncate mt-0.5">${escapeDemandsHtml(t.title)}</p>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onclick="openGrillModal('${escapeDemandsHtml(t.id)}')"
            title="Refinar Demanda no Grill (Q&A)"
            class="px-2 py-1 rounded-lg bg-indigo-900/40 hover:bg-indigo-600 border border-indigo-500/30 text-indigo-300 hover:text-white text-[11px] font-mono transition-all flex items-center gap-1">
            <span>🔥</span> Grill
          </button>
          <button
            type="button"
            onclick="trackDemandInRoadmap('${escapeDemandsHtml(t.id)}')"
            title="Ver no Roadmap"
            class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-cyan-300 text-xs transition-colors">
            🗺️
          </button>
        </div>
      </div>
    `).join("");
  } catch (err) {
    console.warn("Error loading recent demands:", err);
  }
}

function resetDemandForm() {
  const ids = [
    "demand-title-input",
    "demand-problem-input",
    "demand-journey-input",
    "demand-nongoals-input",
    "demand-criteria-input",
  ];
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
}

function escapeDemandsHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function jsonStringify(obj) {
  return JSON.stringify(obj);
}

let currentGrillSession = null;

async function openGrillModal(ticketId) {
  const modal = document.getElementById("demand-grill-modal");
  if (!modal) return;

  const idEl = document.getElementById("grill-modal-ticket-id");
  const titleEl = document.getElementById("grill-modal-ticket-title");
  const bodyEl = document.getElementById("grill-modal-body");

  if (idEl) idEl.textContent = ticketId;
  if (titleEl) {
    const existing = demandsUi.recentTickets.find((t) => t.id === ticketId);
    titleEl.textContent = existing ? existing.title : "Carregando...";
  }

  if (bodyEl) {
    bodyEl.innerHTML = `
      <div class="text-center p-8 space-y-3">
        <span class="text-3xl animate-spin inline-block">⏳</span>
        <p class="text-slate-400 font-mono">Pesquisando documentação e sintetizando perguntas essenciais de Grill ($0.00)...</p>
      </div>
    `;
  }

  modal.classList.remove("hidden");
  document.body.classList.add("overflow-hidden");

  try {
    const res = await fetch(`/api/demands/tickets/${encodeURIComponent(ticketId)}/grill`, {
      method: "POST",
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Falha ao iniciar sessão de Grill");
    }
    const session = await res.json();
    currentGrillSession = session;
    renderGrillModalSession(session);
  } catch (err) {
    console.error("Grill error:", err);
    if (bodyEl) {
      bodyEl.innerHTML = `
        <div class="p-4 rounded-xl bg-rose-950/40 border border-rose-500/30 text-rose-300 space-y-2">
          <p class="font-semibold">Erro ao carregar Grill da demanda:</p>
          <p class="text-xs text-rose-200/80 font-mono">${escapeDemandsHtml(err.message)}</p>
          <button type="button" onclick="openGrillModal('${escapeDemandsHtml(ticketId)}')" class="px-3 py-1.5 rounded-lg bg-slate-800 text-white text-xs">Tentar Novamente</button>
        </div>
      `;
    }
  }
}

function closeGrillModal() {
  const modal = document.getElementById("demand-grill-modal");
  if (modal) modal.classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
  currentGrillSession = null;
}

function renderGrillModalSession(session) {
  const bodyEl = document.getElementById("grill-modal-body");
  if (!bodyEl) return;

  let insightsHtml = "";
  if (session.doc_insights?.length) {
    insightsHtml = `
      <div class="p-3.5 rounded-xl bg-indigo-950/30 border border-indigo-500/20 text-indigo-300 space-y-1.5">
        <div class="flex items-center gap-1.5 font-semibold text-xs text-indigo-200">
          <span>📚</span> Insights da Documentação do Projeto:
        </div>
        <ul class="list-disc list-inside space-y-0.5 text-[11px] text-indigo-200/80 font-mono">
          ${session.doc_insights.map((ins) => `<li>${escapeDemandsHtml(ins)}</li>`).join("")}
        </ul>
      </div>
    `;
  }

  const questionsHtml = session.questions.map((q, qIdx) => `
    <div class="p-4 rounded-xl bg-slate-950/60 border border-slate-800 space-y-3" data-question-id="${escapeDemandsHtml(q.id)}">
      <div>
        <span class="text-[10px] uppercase font-mono text-indigo-400 font-bold tracking-wider">Questão ${qIdx + 1} • ${escapeDemandsHtml(q.category)}</span>
        <h4 class="text-xs font-semibold text-slate-100 mt-0.5">${escapeDemandsHtml(q.question)}</h4>
        ${q.context_reason ? `<p class="text-[11px] text-slate-400 mt-0.5">${escapeDemandsHtml(q.context_reason)}</p>` : ""}
      </div>

      <div class="space-y-2">
        ${q.options.map((opt, oIdx) => `
          <label class="flex items-start gap-2.5 p-2.5 rounded-lg bg-slate-900/60 border border-slate-800 hover:border-slate-700 cursor-pointer transition-colors">
            <input 
              type="radio" 
              name="grill_q_${escapeDemandsHtml(q.id)}" 
              value="${escapeDemandsHtml(opt.label)}" 
              ${opt.is_recommended ? "checked" : ""}
              class="mt-0.5 text-emerald-500 focus:ring-0 bg-slate-950 border-slate-700"
            />
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="text-xs text-slate-200 font-medium">${escapeDemandsHtml(opt.label)}</span>
                ${opt.is_recommended ? '<span class="px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[9px] font-mono">Recomendado</span>' : ""}
              </div>
              ${opt.description ? `<p class="text-[10px] text-slate-400 mt-0.5">${escapeDemandsHtml(opt.description)}</p>` : ""}
            </div>
          </label>
        `).join("")}
      </div>

      ${q.allow_custom_input ? `
        <div class="pt-1">
          <input 
            type="text" 
            id="grill_custom_${escapeDemandsHtml(q.id)}" 
            placeholder="Ou digite uma resposta personalizada..." 
            class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-[11px] text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 font-sans"
          />
        </div>
      ` : ""}
    </div>
  `).join("");

  bodyEl.innerHTML = `
    ${insightsHtml}
    <div class="space-y-4">
      ${questionsHtml}
    </div>
  `;
}

async function submitGrillModalAnswers(autoAccept = false) {
  if (!currentGrillSession) return;

  const ticketId = currentGrillSession.ticket_id;
  const answers = {};

  if (autoAccept) {
    for (const q of currentGrillSession.questions) {
      const rec = q.options.find((opt) => opt.is_recommended) || q.options[0];
      if (rec) answers[q.id] = rec.label;
    }
  } else {
    for (const q of currentGrillSession.questions) {
      const customInput = document.getElementById(`grill_custom_${q.id}`);
      const customVal = customInput?.value.trim();
      if (customVal) {
        answers[q.id] = customVal;
      } else {
        const checked = document.querySelector(`input[name="grill_q_${q.id}"]:checked`);
        if (checked) {
          answers[q.id] = checked.value;
        } else {
          const rec = q.options.find((opt) => opt.is_recommended) || q.options[0];
          if (rec) answers[q.id] = rec.label;
        }
      }
    }
  }

  const btn = document.getElementById("btn-grill-submit");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin inline-block mr-1">⏳</span> Refinando...';
  }

  try {
    const res = await fetch(`/api/demands/tickets/${encodeURIComponent(ticketId)}/grill/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: jsonStringify({ answers: answers, auto_accept_unanswered: true }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Falha ao aplicar refinamentos do Grill");
    }

    const result = await res.json();
    showToast(`Ticket ${result.ticket_id} refinado com sucesso! (${result.summary_of_changes?.length || 0} alterações)`, "success");
    closeGrillModal();
    loadRecentDemands();
    if (typeof loadRoadmapSnapshot === "function") {
      loadRoadmapSnapshot();
    }
  } catch (err) {
    console.error("Submit grill error:", err);
    showToast(`Erro ao aplicar respostas: ${err.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '💾 Aplicar Refinamento';
    }
  }
}
