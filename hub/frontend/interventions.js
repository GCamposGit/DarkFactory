/**
 * DarkHub - Priority Interventions & Human-in-the-Loop Cockpit (USR-58).
 *
 * Governs the Action Banner / Hero Strip on top of the cockpit and the
 * Interactive Grill Modal, consolidating Grills, G8 Dokploy Deploys, and WAITING_HUMAN blockers.
 */

const interventionsState = {
  loading: false,
  report: null,
  activeGrillSession: null,
};

document.addEventListener("DOMContentLoaded", () => {
  loadPriorityInterventions();

  // Check URL hash for direct grill navigation (e.g. #grill=USR-09)
  function handleUrlHash() {
    const hash = window.location.hash || "";
    const match = hash.match(/^#grill=([A-Za-z0-9_-]+)$/);
    if (match && match[1]) {
      setTimeout(() => openGrillModal(match[1]), 150);
    }
  }
  handleUrlHash();
  window.addEventListener("hashchange", handleUrlHash);

  // Bind keyboard Escape for grill modal
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("grill-modal")?.classList.contains("hidden")) {
      closeGrillModal();
    }
  });

  // Periodic poll for priority interventions (every 30s)
  setInterval(() => {
    if (!document.hidden && !interventionsState.loading) {
      loadPriorityInterventions();
    }
  }, 30000);
});

function escapeInterventionsHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function loadPriorityInterventions(force = false) {
  if (interventionsState.loading && !force) return;
  interventionsState.loading = true;

  try {
    const request = typeof hubFetch === "function" ? hubFetch : fetch;
    const project = window.currentActiveProjectId || "darkfac";
    const query = project ? `?project_id=${encodeURIComponent(project)}` : "";
    let response = await request(`/api/interventions/priority${query}`, {
      headers: { Accept: "application/json" },
    });
    if (response.status === 404) {
      response = await request(`/interventions/priority${query}`, {
        headers: { Accept: "application/json" },
      });
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    interventionsState.report = await response.json();
    renderInterventionsHeroStrip();
  } catch (err) {
    console.warn("Could not load priority interventions:", err);
  } finally {
    interventionsState.loading = false;
  }
}

function renderInterventionsHeroStrip() {
  const container = document.getElementById("interventions-hero-strip");
  if (!container) return;

  const report = interventionsState.report || { total_count: 0, items: [] };
  const total = report.total_count || 0;

  if (total === 0) {
    container.innerHTML = `
      <div class="rounded-2xl border border-emerald-500/20 bg-emerald-950/20 px-4 py-2.5 flex items-center justify-between shadow-sm">
        <div class="flex items-center gap-3">
          <div class="w-2.5 h-2.5 rounded-full bg-emerald-400"></div>
          <div>
            <span class="text-xs font-semibold text-emerald-300">Zero Pendências de Intervenção Humana</span>
            <span class="text-[11px] text-slate-400 ml-2 hidden sm:inline">A fábrica autônoma está operando sem bloqueios no momento.</span>
          </div>
        </div>
        <button 
          onclick="loadPriorityInterventions(true)" 
          title="Verificar novamente"
          class="text-[11px] text-slate-400 hover:text-slate-200 transition-colors px-2 py-1 rounded bg-slate-900/60 border border-slate-800">
          ↻ Atualizar
        </button>
      </div>
    `;
    return;
  }

  // Interventions pending -> Render highlighted action strip
  const grillCount = report.grill_count || 0;
  const deployCount = report.deploy_count || 0;
  const waitingCount = report.waiting_human_count || 0;

  const itemsHtml = (report.items || []).map((item) => {
    let badgeClass = "bg-amber-500/10 text-amber-300 border-amber-500/30";
    let icon = "🔥";
    let actionBtn = "";

    if (item.kind === "grill") {
      badgeClass = "bg-amber-500/10 text-amber-300 border-amber-500/30";
      icon = "🔥 Grill";
      actionBtn = `
        <button 
          onclick="openGrillModal('${escapeInterventionsHtml(item.action_target_id)}')"
          class="px-3 py-1 rounded-lg bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 text-slate-950 font-bold text-xs shadow-md shadow-amber-500/20 transition-all flex items-center gap-1.5 cursor-pointer">
          <span>Responder Grill</span>
          <span>→</span>
        </button>
      `;
    } else if (item.kind === "deploy_g8") {
      badgeClass = "bg-rose-500/10 text-rose-300 border-rose-500/30";
      icon = "🚀 Deploy G8";
      actionBtn = `
        <button 
          onclick="document.getElementById('enterprise-security-section')?.scrollIntoView({behavior: 'smooth'})"
          class="px-3 py-1 rounded-lg bg-gradient-to-r from-rose-500 to-pink-500 hover:from-rose-400 hover:to-pink-400 text-white font-bold text-xs shadow-md shadow-rose-500/20 transition-all flex items-center gap-1.5 cursor-pointer">
          <span>Aprovar Deploy</span>
          <span>→</span>
        </button>
      `;
    } else {
      badgeClass = "bg-indigo-500/10 text-indigo-300 border-indigo-500/30";
      icon = "🛑 WAITING_HUMAN";
      actionBtn = `
        <button 
          onclick="document.getElementById('tasks-dashboard-section')?.scrollIntoView({behavior: 'smooth'})"
          class="px-3 py-1 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs shadow-md shadow-indigo-600/20 transition-all flex items-center gap-1.5 cursor-pointer">
          <span>Ver Tarefa</span>
          <span>→</span>
        </button>
      `;
    }

    return `
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-3 rounded-xl bg-slate-900/80 border border-slate-800/80 hover:border-amber-500/30 transition-all">
        <div class="space-y-1">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="px-2 py-0.5 rounded text-[10px] font-mono uppercase font-semibold border ${badgeClass}">
              ${icon}
            </span>
            <span class="text-xs font-bold text-slate-100">${escapeInterventionsHtml(item.title)}</span>
            <span class="text-[10px] text-slate-500 font-mono">(${escapeInterventionsHtml(item.project_id)})</span>
          </div>
          <p class="text-[11px] text-slate-400 leading-snug">${escapeInterventionsHtml(item.description)}</p>
        </div>
        <div class="shrink-0 flex items-center gap-2">
          ${actionBtn}
        </div>
      </div>
    `;
  }).join("");

  container.innerHTML = `
    <div class="rounded-2xl border border-amber-500/30 bg-gradient-to-b from-amber-500/10 via-slate-900/90 to-slate-950 p-4 sm:p-5 space-y-4 shadow-xl shadow-amber-500/5 relative overflow-hidden">
      <!-- Glow background -->
      <div class="absolute -right-12 -top-12 w-48 h-48 bg-amber-500/10 rounded-full blur-2xl pointer-events-none"></div>

      <!-- Header Row -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5">
        <div class="flex items-center gap-3">
          <span class="relative flex h-3.5 w-3.5">
            <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
            <span class="relative inline-flex rounded-full h-3.5 w-3.5 bg-amber-500"></span>
          </span>
          <div>
            <h2 class="text-sm font-bold text-white flex items-center gap-2">
              <span>Seção Prioritária: Intervenções Pendentes</span>
              <span class="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30 text-xs font-mono font-bold">
                ${total} ${total === 1 ? "ação pendente" : "ações pendentes"}
              </span>
            </h2>
            <p class="text-[11px] text-slate-400">Ações prioritárias aguardando decisão humana para desbloquear o avanço autônomo da fábrica.</p>
          </div>
        </div>

        <div class="flex items-center gap-2 text-[11px] font-mono text-slate-400">
          ${grillCount ? `<span class="px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/20 text-amber-300">${grillCount} Grill(s)</span>` : ""}
          ${deployCount ? `<span class="px-2 py-0.5 rounded bg-rose-500/10 border border-rose-500/20 text-rose-300">${deployCount} Deploy(s)</span>` : ""}
          ${waitingCount ? `<span class="px-2 py-0.5 rounded bg-indigo-500/10 border border-indigo-500/20 text-indigo-300">${waitingCount} Bloqueio(s)</span>` : ""}
          <button 
            onclick="loadPriorityInterventions(true)" 
            title="Recarregar fila prioritária"
            class="p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition-colors">
            ↻
          </button>
        </div>
      </div>

      <!-- Items Queue -->
      <div class="space-y-2">
        ${itemsHtml}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Interactive Grill Modal Controller
// ---------------------------------------------------------------------------

async function openGrillModal(ticketId) {
  const modal = document.getElementById("grill-modal");
  const content = document.getElementById("grill-modal-content");
  const titleEl = document.getElementById("grill-modal-title");
  const subtitleEl = document.getElementById("grill-modal-subtitle");
  if (!modal || !content) return;

  modal.classList.remove("hidden");
  document.body.classList.add("overflow-hidden");

  if (titleEl) titleEl.textContent = `Grill de Demanda: [${ticketId}]`;
  if (subtitleEl) subtitleEl.textContent = "Carregando perguntas essenciais de desambiguação...";
  content.innerHTML = `
    <div class="flex flex-col items-center justify-center py-16 space-y-3">
      <div class="animate-spin text-3xl">🔥</div>
      <p class="text-xs font-mono text-slate-400">Iniciando sessão cirúrgica de Grill ($0)...</p>
    </div>
  `;

  try {
    const request = typeof hubFetch === "function" ? hubFetch : fetch;
    const response = await request(`/api/demands/tickets/${encodeURIComponent(ticketId)}/grill?force_heuristic=true`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: Falha ao iniciar Grill`);
    }
    const session = await response.json();
    interventionsState.activeGrillSession = session;
    renderGrillQuestions(session);
  } catch (err) {
    content.innerHTML = `
      <div class="p-6 rounded-xl border border-rose-500/30 bg-rose-500/10 text-rose-300 text-xs space-y-2">
        <p class="font-bold">❌ Erro ao abrir sessão de Grill:</p>
        <p class="font-mono text-[11px]">${escapeInterventionsHtml(err.message)}</p>
        <button onclick="closeGrillModal()" class="px-3 py-1 rounded bg-slate-800 text-white text-xs mt-3">Fechar</button>
      </div>
    `;
  }
}

function closeGrillModal() {
  const modal = document.getElementById("grill-modal");
  if (modal) modal.classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
  interventionsState.activeGrillSession = null;
  // Clear URL hash if present
  if (window.location.hash.startsWith("#grill=")) {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

function renderGrillQuestions(session) {
  const content = document.getElementById("grill-modal-content");
  const subtitleEl = document.getElementById("grill-modal-subtitle");
  if (!content) return;

  if (subtitleEl) {
    subtitleEl.textContent = `Responda às questões cirúrgicas para delimitar o escopo e autorizar a execução.`;
  }

  const questions = session.questions || [];
  if (!questions.length) {
    content.innerHTML = `
      <div class="p-8 text-center text-xs text-slate-400">
        Nenhuma questão pendente para esta demanda. O ticket está pronto para execução.
      </div>
    `;
    return;
  }

  const questionsHtml = questions.map((q, qIdx) => {
    const optionsHtml = (q.options || []).map((opt) => {
      const isRec = Boolean(opt.is_recommended);
      return `
        <label class="flex items-start gap-3 p-3 rounded-xl border border-slate-800 bg-slate-900/60 hover:bg-slate-900 hover:border-indigo-500/40 transition-all cursor-pointer group">
          <input 
            type="radio" 
            name="grill_q_${escapeInterventionsHtml(q.id)}" 
            value="${escapeInterventionsHtml(opt.label)}" 
            ${isRec ? "checked" : ""}
            class="mt-1 text-indigo-600 focus:ring-indigo-500 bg-slate-800 border-slate-700 cursor-pointer">
          <div class="space-y-1">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-xs font-semibold text-slate-200 group-hover:text-white transition-colors">${escapeInterventionsHtml(opt.label)}</span>
              ${isRec ? '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">⭐ Recomendado (Staff+)</span>' : ""}
            </div>
            ${opt.description ? `<p class="text-[11px] text-slate-400">${escapeInterventionsHtml(opt.description)}</p>` : ""}
          </div>
        </label>
      `;
    }).join("");

    return `
      <div class="p-5 rounded-2xl border border-slate-800/80 bg-slate-950/60 space-y-3.5">
        <div class="space-y-1">
          <div class="flex items-center gap-2">
            <span class="w-5 h-5 rounded-full bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 text-[11px] font-mono font-bold flex items-center justify-center">
              ${qIdx + 1}
            </span>
            <span class="text-[10px] font-mono uppercase tracking-wider text-slate-500 font-semibold">${escapeInterventionsHtml(q.category || "escopo")}</span>
          </div>
          <h3 class="text-xs font-bold text-slate-100">${escapeInterventionsHtml(q.question)}</h3>
          ${q.context_reason ? `<p class="text-[11px] text-amber-300/80 bg-amber-500/10 px-2.5 py-1 rounded-lg border border-amber-500/20 inline-block font-mono">💡 Motivo: ${escapeInterventionsHtml(q.context_reason)}</p>` : ""}
        </div>

        <!-- Options List -->
        <div class="space-y-2">
          ${optionsHtml}
        </div>

        <!-- Custom Write-in text -->
        ${q.allow_custom_input ? `
          <div class="pt-2">
            <label class="text-[10px] font-mono text-slate-400 block mb-1">Ou escreva uma diretriz customizada para esta questão:</label>
            <input 
              type="text" 
              id="custom_q_${escapeInterventionsHtml(q.id)}" 
              placeholder="Diretriz customizada opcional..." 
              class="w-full px-3 py-1.5 rounded-xl bg-slate-900 border border-slate-800 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-colors">
          </div>
        ` : ""}
      </div>
    `;
  }).join("");

  content.innerHTML = `
    <div class="space-y-6">
      <!-- Questions Container -->
      <div class="space-y-4">
        ${questionsHtml}
      </div>

      <!-- Action Footer inside Modal -->
      <div class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-4 border-t border-slate-800">
        <div class="flex items-center gap-2">
          <button 
            type="button" 
            onclick="autoSelectRecommendedGrillOptions()"
            class="px-3 py-1.5 rounded-xl border border-slate-700 bg-slate-900 text-slate-300 hover:text-white hover:border-slate-600 text-xs transition-colors">
            ✨ Auto-adotar Recomendadas
          </button>
          <button 
            type="button" 
            onclick="closeGrillModal()"
            class="px-3 py-1.5 rounded-xl text-slate-400 hover:text-white text-xs transition-colors">
            Cancelar
          </button>
        </div>

        <button 
          id="btn-submit-grill-modal"
          type="button" 
          onclick="submitGrillModalAnswers('${escapeInterventionsHtml(session.ticket_id)}')"
          class="w-full sm:w-auto px-5 py-2 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-slate-950 font-bold text-xs shadow-lg shadow-emerald-500/20 transition-all flex items-center justify-center gap-2 cursor-pointer">
          <span>Salvar Respostas e Refinar Ticket</span>
          <span>✓</span>
        </button>
      </div>
    </div>
  `;
}

function autoSelectRecommendedGrillOptions() {
  const session = interventionsState.activeGrillSession;
  if (!session) return;
  (session.questions || []).forEach((q) => {
    const radios = document.getElementsByName(`grill_q_${q.id}`);
    const rec = (q.options || []).find((o) => o.is_recommended);
    if (rec && radios) {
      for (const r of radios) {
        if (r.value === rec.label) {
          r.checked = true;
          break;
        }
      }
    }
  });
}

async function submitGrillModalAnswers(ticketId) {
  const session = interventionsState.activeGrillSession;
  if (!session) return;

  const btn = document.getElementById("btn-submit-grill-modal");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="animate-spin inline-block mr-1">⏳</span> Refinando ticket...';
  }

  const answers = {};
  (session.questions || []).forEach((q) => {
    const customInput = document.getElementById(`custom_q_${q.id}`);
    const customVal = customInput?.value.trim();
    if (customVal) {
      answers[q.id] = customVal;
    } else {
      const radios = document.getElementsByName(`grill_q_${q.id}`);
      let selected = "";
      for (const r of radios) {
        if (r.checked) {
          selected = r.value;
          break;
        }
      }
      if (selected) {
        answers[q.id] = selected;
      }
    }
  });

  try {
    const request = typeof hubFetch === "function" ? hubFetch : fetch;
    let response = await request(`/api/demands/tickets/${encodeURIComponent(ticketId)}/grill/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        answers: answers,
        auto_accept_unanswered: true,
      }),
    });

    if (response.status === 401) {
      // Direct session renewal fallback
      try {
        const sRes = await fetch("/api/session");
        if (sRes.ok) {
          const sData = await sRes.json();
          window.sessionToken = sData.session_token;
          if (typeof state !== "undefined") state.sessionToken = sData.session_token;
          try { localStorage.setItem("darkhub_session_token", sData.session_token); } catch (_) {}
          response = await fetch(`/api/demands/tickets/${encodeURIComponent(ticketId)}/grill/submit`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json",
              "X-Hub-Session": sData.session_token,
            },
            body: JSON.stringify({
              answers: answers,
              auto_accept_unanswered: true,
            }),
          });
        }
      } catch (_) {}
    }

    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }

    const result = await response.json();
    if (typeof showToast === "function") {
      showToast(`Grill concluído com sucesso para [${ticketId}]. Ticket refinado!`, "success");
    }

    closeGrillModal();
    // Refresh priority interventions and demands lists
    loadPriorityInterventions(true);
    if (typeof loadRecentDemands === "function") {
      loadRecentDemands();
    }
  } catch (err) {
    alert(`Erro ao submeter respostas do Grill: ${err.message}`);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = 'Salvar Respostas e Refinar Ticket ✓';
    }
  }
}

// Export functions to global scope
window.openGrillModal = openGrillModal;
window.closeGrillModal = closeGrillModal;
window.loadPriorityInterventions = loadPriorityInterventions;
window.autoSelectRecommendedGrillOptions = autoSelectRecommendedGrillOptions;
window.submitGrillModalAnswers = submitGrillModalAnswers;
