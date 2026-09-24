/**
 * Cross-Project Reusable Catalog & Factory Self-Evolution (HF-25 / DH-04) for DarkHub.
 * Enables synchronization of catalog components across registered projects,
 * creation of evolution proposals, holdout evaluation, promotion with rollback snapshots,
 * and deterministic rollback.
 */

const catalogState = {
  components: [],
  evolution: null,
  proposals: [],
  loading: false,
  syncingId: null,
  evaluatingId: null,
  promotingId: null,
  rollingBackId: null,
};

function getSessionToken() {
  return window.state?.sessionToken || null;
}

async function authenticatedFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getSessionToken();
  if (token) {
    headers.set("X-Hub-Session", token);
  }
  return fetch(url, { ...options, headers });
}

function initCatalog() {
  mountCatalogSection();
  loadCatalogData();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initCatalog);
} else {
  initCatalog();
}

function mountCatalogSection() {
  if (document.getElementById("cross-catalog-section")) return;
  const targetAnchor =
    document.getElementById("infrastructure-cards-section") ||
    document.getElementById("api-credits-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "cross-catalog-section";
  section.className =
    "space-y-4 rounded-2xl border border-emerald-900/40 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-emerald-950/10";
  section.innerHTML = `
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400 text-sm">📦</span>
          <h2 class="text-base font-semibold text-slate-100">Catálogo Cross-Projeto & Auto-Evolução (HF-25 / DH-04)</h2>
          <span class="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 border border-emerald-500/20">Ações Habilitadas</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">Componentes reutilizáveis compartilhados entre Atrium, Jarvis e DarkFac com salvaguardas de auto-evolução.</p>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <button id="btn-open-propose-modal" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-3 py-1.5 text-xs font-medium text-indigo-300 hover:bg-indigo-500/20 transition">
          <span>✨ Nova Proposta</span>
        </button>
        <button id="refresh-catalog-btn" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>🔄 Atualizar</span>
        </button>
      </div>
    </div>

    <!-- Components Grid -->
    <div>
      <h3 class="text-xs font-mono uppercase tracking-wider text-slate-400 mb-2">Componentes Reutilizáveis (Sincronização Ativa)</h3>
      <div id="catalog-components-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        <div class="col-span-full py-4 text-center text-xs text-slate-500">Carregando componentes reutilizáveis...</div>
      </div>
    </div>

    <!-- Evolution Status & Proposals -->
    <div id="evolution-status-container" class="mt-6 pt-4 border-t border-slate-800/80 text-xs text-slate-400 space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="font-medium text-slate-300">Subsistema de Auto-Evolução:</span>
          <span id="evolution-summary-stats" class="text-slate-400">Consultando...</span>
        </div>
      </div>
      <div id="evolution-proposals-list" class="space-y-2">
        <div class="py-2 text-slate-500 font-mono text-[11px]">Carregando propostas de evolução...</div>
      </div>
    </div>

    <!-- Propose Mutation Modal -->
    <div id="evolution-propose-modal" class="hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-lg rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 shadow-2xl">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <h3 class="text-sm font-semibold text-white">Propor Mutação Evolutiva (Self-Evolution)</h3>
          <button type="button" onclick="closeProposeModal()" class="text-slate-400 hover:text-white">✕</button>
        </div>
        <form id="form-evolution-propose" class="space-y-3">
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Categoria (Target Kind)</label>
            <select id="propose-kind" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono">
              <option value="skill_instruction">skill_instruction (Instrução Operacional)</option>
              <option value="context_rule">context_rule (Regra de Contexto / Governance)</option>
              <option value="archetype_template">archetype_template (Template de Arquétipo)</option>
              <option value="routing_config">routing_config (Configuração de Roteador)</option>
            </select>
          </div>
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Caminho Relativo no Repositório (Target Path)</label>
            <input id="propose-path" type="text" placeholder=".agents/skills/04-autonomous-piv-loop/SKILL.md" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono" />
          </div>
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Gatilho (Trigger)</label>
            <select id="propose-trigger" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono">
              <option value="manual_proposal">manual_proposal (Proposta Manual pelo Operador)</option>
              <option value="rules_drift">rules_drift (Correção de Desvio de Regras)</option>
              <option value="rca_discovery">rca_discovery (Descoberta Pós-Incidente / RCA)</option>
              <option value="fail_repeated">fail_repeated (Falhas Repetidas no Harness)</option>
              <option value="benchmark_shift">benchmark_shift (Mudança na Fronteira de Pareto)</option>
            </select>
          </div>
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Justificativa e Evidência (Rationale)</label>
            <textarea id="propose-rationale" rows="2" placeholder="Descreva a falha ou otimização empírica observada..." required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono"></textarea>
          </div>
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Conteúdo do Patch ou Substituição (Patch Content)</label>
            <textarea id="propose-patch" rows="4" placeholder="# Patch content..." required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono"></textarea>
          </div>
          <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-800">
            <button type="button" onclick="closeProposeModal()" class="px-3 py-1.5 rounded-xl bg-slate-800 text-slate-300 text-xs font-medium hover:bg-slate-700">Cancelar</button>
            <button type="submit" id="btn-submit-proposal" class="px-3 py-1.5 rounded-xl bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-500 shadow-md">Registrar Proposta</button>
          </div>
        </form>
      </div>
    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  document.getElementById("refresh-catalog-btn")?.addEventListener("click", () => loadCatalogData());
  document.getElementById("btn-open-propose-modal")?.addEventListener("click", () => openProposeModal());
  document.getElementById("form-evolution-propose")?.addEventListener("submit", handleProposeSubmit);
}

function openProposeModal() {
  document.getElementById("evolution-propose-modal")?.classList.remove("hidden");
}

function closeProposeModal() {
  document.getElementById("evolution-propose-modal")?.classList.add("hidden");
}

async function loadCatalogData() {
  catalogState.loading = true;
  try {
    const [compRes, evoRes] = await Promise.all([
      fetch("/api/catalog/components").then((r) => r.ok ? r.json() : { components: [] }),
      fetch("/api/evolution/status").then((r) => r.ok ? r.json() : null),
    ]);

    catalogState.components = compRes.components || [];
    catalogState.evolution = evoRes;
    catalogState.proposals = evoRes?.proposals || [];
    renderCatalogUI();
  } catch (err) {
    console.error("Failed to load catalog/evolution data:", err);
  } finally {
    catalogState.loading = false;
  }
}

function renderCatalogUI() {
  const grid = document.getElementById("catalog-components-grid");
  if (!grid) return;

  if (!catalogState.components.length) {
    grid.innerHTML = `<div class="col-span-full py-4 text-center text-xs text-slate-500">Nenhum componente registrado no catálogo cross-projeto.</div>`;
  } else {
    grid.innerHTML = catalogState.components.map((c) => {
      const isSyncing = catalogState.syncingId === c.id;
      return `
        <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5 flex flex-col justify-between hover:border-slate-700 transition">
          <div>
            <div class="flex items-center justify-between mb-1.5">
              <span class="text-xs font-semibold text-slate-200">${escapeHtml(c.name)}</span>
              <span class="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-mono text-slate-400">v${escapeHtml(c.version)}</span>
            </div>
            <p class="text-[11px] text-slate-400 leading-relaxed mb-3">${escapeHtml(c.description)}</p>
          </div>
          
          <div class="space-y-2 pt-2 border-t border-slate-800/50">
            <div class="flex items-center justify-between text-[10px] text-slate-500">
              <span>Origem: <strong class="text-slate-400">${escapeHtml(c.source_project_id)}</strong></span>
              <span class="rounded bg-emerald-950/40 px-1.5 py-0.5 text-emerald-400 font-mono">${escapeHtml(c.kind)}</span>
            </div>

            <!-- Sync Action Form -->
            <div class="flex items-center gap-1.5 pt-1">
              <select id="sync-target-${escapeHtml(c.id)}" class="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-2 py-1 text-[11px] font-mono text-slate-300">
                <option value="site-ggcampos">site-ggcampos (Atrium)</option>
                <option value="segundo-cerebro">segundo-cerebro</option>
                <option value="jarvis">jarvis</option>
                <option value="darkfac">darkfac (Core)</option>
              </select>
              <button 
                type="button"
                onclick="syncCatalogComponent('${escapeHtml(c.id)}')"
                ${isSyncing ? "disabled" : ""}
                class="px-2.5 py-1 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30 text-[11px] font-medium transition active:scale-95 disabled:opacity-50">
                ${isSyncing ? "Sincronizando..." : "Sincronizar"}
              </button>
            </div>
            <div id="sync-feedback-${escapeHtml(c.id)}" class="text-[10px] font-mono hidden"></div>
          </div>
        </div>
      `;
    }).join("");
  }

  // Render Evolution proposals and stats
  const statsEl = document.getElementById("evolution-summary-stats");
  if (statsEl && catalogState.evolution) {
    const evo = catalogState.evolution;
    statsEl.innerHTML = `
      <span class="font-mono text-slate-300">Total: <strong>${evo.total_proposals}</strong></span> · 
      <span class="font-mono text-emerald-400">Ativas: <strong>${evo.active_promotions}</strong></span> · 
      <span class="font-mono text-rose-400">Rejeitadas: <strong>${evo.rejected_count}</strong></span>
    `;
  }

  const proposalsList = document.getElementById("evolution-proposals-list");
  if (proposalsList) {
    if (!catalogState.proposals.length) {
      proposalsList.innerHTML = `<div class="py-2 text-slate-500 text-xs">Nenhuma proposta de mutação evolutiva registrada no momento.</div>`;
    } else {
      proposalsList.innerHTML = catalogState.proposals.map((p) => {
        const isEval = catalogState.evaluatingId === p.id;
        const isProm = catalogState.promotingId === p.id;
        const isRoll = catalogState.rollingBackId === p.id;

        let statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-300">${p.status}</span>`;
        if (p.status === "promoted") {
          statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-950/60 text-emerald-400 border border-emerald-700/40">Promovido</span>`;
        } else if (p.status === "evaluated") {
          statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-950/60 text-indigo-300 border border-indigo-700/40">Avaliado (Holdout)</span>`;
        } else if (p.status === "rolled_back") {
          statusBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-amber-950/60 text-amber-300 border border-amber-700/40">Revertido</span>`;
        }

        return `
          <div class="p-3 rounded-xl border border-slate-800 bg-slate-950/40 flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div class="space-y-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-mono font-semibold text-slate-200 text-xs">${escapeHtml(p.id)}</span>
                ${statusBadge}
                <span class="text-[10px] text-slate-400 font-mono">${escapeHtml(p.target_path)}</span>
              </div>
              <p class="text-[11px] text-slate-400">${escapeHtml(p.rationale || "Sem justificativa")}</p>
            </div>
            
            <div class="flex items-center gap-1.5 self-end md:self-auto flex-wrap">
              <button 
                type="button" 
                onclick="evaluateEvolutionProposal('${escapeHtml(p.id)}')"
                ${isEval ? "disabled" : ""}
                class="px-2.5 py-1 rounded-lg border border-indigo-500/40 bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/20 text-[11px] font-mono transition disabled:opacity-50">
                ${isEval ? "Avaliando..." : "Avaliar"}
              </button>
              
              <button 
                type="button" 
                onclick="promoteEvolutionProposal('${escapeHtml(p.id)}')"
                ${isProm || p.status === "promoted" ? "disabled" : ""}
                class="px-2.5 py-1 rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20 text-[11px] font-mono transition disabled:opacity-40">
                ${isProm ? "Promovendo..." : "Promover"}
              </button>

              <button 
                type="button" 
                onclick="rollbackEvolutionProposal('${escapeHtml(p.id)}')"
                ${isRoll || p.status !== "promoted" ? "disabled" : ""}
                class="px-2.5 py-1 rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 text-[11px] font-mono transition disabled:opacity-40">
                ${isRoll ? "Revertendo..." : "Reverter"}
              </button>
            </div>
          </div>
        `;
      }).join("");
    }
  }
}

async function syncCatalogComponent(componentId) {
  const targetSelect = document.getElementById(`sync-target-${componentId}`);
  const feedback = document.getElementById(`sync-feedback-${componentId}`);
  const targetProjectId = targetSelect?.value || "site-ggcampos";

  catalogState.syncingId = componentId;
  renderCatalogUI();

  try {
    const res = await authenticatedFetch("/api/catalog/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        component_id: componentId,
        target_project_id: targetProjectId,
        overwrite: true,
      }),
    });

    const data = await res.json();
    if (res.ok && data.success) {
      if (feedback) {
        feedback.className = "text-[10px] font-mono text-emerald-400 block pt-1";
        feedback.textContent = `✓ Sincronizado para ${targetProjectId} (${data.result?.copied_files?.length || 1} arquivos)`;
      }
    } else {
      if (feedback) {
        feedback.className = "text-[10px] font-mono text-rose-400 block pt-1";
        feedback.textContent = `✗ Falha: ${data.detail || "Erro de sincronização"}`;
      }
    }
  } catch (err) {
    if (feedback) {
      feedback.className = "text-[10px] font-mono text-rose-400 block pt-1";
      feedback.textContent = `✗ Erro de rede: ${err.message}`;
    }
  } finally {
    catalogState.syncingId = null;
    renderCatalogUI();
  }
}

async function handleProposeSubmit(e) {
  e.preventDefault();
  const kind = document.getElementById("propose-kind")?.value;
  const path = document.getElementById("propose-path")?.value;
  const trigger = document.getElementById("propose-trigger")?.value;
  const rationale = document.getElementById("propose-rationale")?.value;
  const patch = document.getElementById("propose-patch")?.value;

  const btn = document.getElementById("btn-submit-proposal");
  if (btn) btn.textContent = "Registrando...";

  try {
    const res = await authenticatedFetch("/api/evolution/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_kind: kind,
        target_path: path,
        trigger: trigger,
        patch_content: patch,
        rationale: rationale,
      }),
    });

    if (res.ok) {
      closeProposeModal();
      document.getElementById("form-evolution-propose")?.reset();
      await loadCatalogData();
    } else {
      const err = await res.json();
      alert(`Falha ao propor mutação: ${err.detail || "Erro desconhecido"}`);
    }
  } catch (err) {
    alert(`Erro de conexão: ${err.message}`);
  } finally {
    if (btn) btn.textContent = "Registrar Proposta";
  }
}

async function evaluateEvolutionProposal(proposalId) {
  catalogState.evaluatingId = proposalId;
  renderCatalogUI();

  try {
    const res = await authenticatedFetch("/api/evolution/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposal_id: proposalId }),
    });

    const data = await res.json();
    if (res.ok) {
      alert(`Avaliação de Holdout (${proposalId}):\nResultado: ${data.success ? "APROVADO [PASS]" : "FALHA [FAIL]"}\nDetalhe: ${data.result?.diagnostic || "Verificação concluída sem anomalias"}`);
      await loadCatalogData();
    } else {
      alert(`Erro na avaliação: ${data.detail || "Falha desconhecida"}`);
    }
  } catch (err) {
    alert(`Erro de rede: ${err.message}`);
  } finally {
    catalogState.evaluatingId = null;
    renderCatalogUI();
  }
}

async function promoteEvolutionProposal(proposalId) {
  catalogState.promotingId = proposalId;
  renderCatalogUI();

  try {
    const res = await authenticatedFetch("/api/evolution/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposal_id: proposalId }),
    });

    const data = await res.json();
    if (res.ok) {
      alert(`Proposta ${proposalId} PROMOVIDA com sucesso!\nSnapshot de rollback criado: ${data.snapshot?.id || "Snapshot retido"}`);
      await loadCatalogData();
    } else {
      alert(`Falha na promoção: ${data.detail || "Erro desconhecido"}`);
    }
  } catch (err) {
    alert(`Erro de rede: ${err.message}`);
  } finally {
    catalogState.promotingId = null;
    renderCatalogUI();
  }
}

async function rollbackEvolutionProposal(proposalId) {
  if (!confirm(`Confirmar reversão (rollback) da mutação ${proposalId}? O arquivo original será restaurado a partir do snapshot.`)) {
    return;
  }

  catalogState.rollingBackId = proposalId;
  renderCatalogUI();

  try {
    const res = await authenticatedFetch("/api/evolution/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposal_id: proposalId }),
    });

    const data = await res.json();
    if (res.ok) {
      alert(`Mutação ${proposalId} revertida com sucesso! Arquivo restaurado.`);
      await loadCatalogData();
    } else {
      alert(`Falha no rollback: ${data.detail || "Erro desconhecido"}`);
    }
  } catch (err) {
    alert(`Erro de rede: ${err.message}`);
  } finally {
    catalogState.rollingBackId = null;
    renderCatalogUI();
  }
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
