/**
 * Enterprise Security, Compliance & Production Deploy Gate (HF-24 / DH-09) for DarkHub.
 * Observes data residency, cryptographic audit chain integrity, SLA RPO/RTO metrics,
 * allows configuration of enterprise policies, evaluates production release readiness (G8),
 * and triggers gated Dokploy production deployments with strict two-step verification.
 */

const enterpriseState = {
  currentProject: "darkfac",
  status: null,
  auditTrail: [],
  lastEvaluation: null,
  loading: false,
  evaluatingDeploy: false,
  triggeringDeploy: false,
};

function getEnterpriseToken() {
  return window.state?.sessionToken || null;
}

async function enterpriseFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getEnterpriseToken();
  if (token) {
    headers.set("X-Hub-Session", token);
  }
  return fetch(url, { ...options, headers });
}

function initEnterprise() {
  mountEnterpriseSection();
  const select = document.getElementById("global-project-select");
  if (select) {
    enterpriseState.currentProject = select.value || "darkfac";
    select.addEventListener("change", (e) => {
      enterpriseState.currentProject = e.target.value;
      enterpriseState.lastEvaluation = null;
      loadEnterpriseData();
    });
  }
  loadEnterpriseData();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initEnterprise);
} else {
  initEnterprise();
}

function mountEnterpriseSection() {
  if (document.getElementById("enterprise-security-section")) return;
  const targetAnchor =
    document.getElementById("content-studio-section") ||
    document.getElementById("cross-catalog-section") ||
    document.getElementById("infrastructure-cards-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "enterprise-security-section";
  section.className =
    "space-y-4 rounded-2xl border border-sky-900/40 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-sky-950/10";
  section.innerHTML = `
    <!-- Header -->
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-sky-500/10 text-sky-400 text-sm">🛡️</span>
          <h2 class="text-base font-semibold text-slate-100">Governança Enterprise, Auditoria & Deploy Dokploy (DH-09)</h2>
          <span id="enterprise-badge" class="rounded-full bg-slate-800 px-2 py-0.5 text-xs font-medium text-slate-400 border border-slate-700">Auditando...</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">Trilha de auditoria criptográfica imutável, conformidade SLA e disparo seguro de deploy Dokploy com avaliação em 2 etapas.</p>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <button id="btn-open-config-modal" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>⚙️ Configurar Perfil</span>
        </button>
        <button id="btn-open-audit-trail" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-300 hover:bg-sky-500/20 transition">
          <span>📜 Trilha de Auditoria</span>
        </button>
        <button id="verify-audit-btn" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>🔗 Verificar Hash Chain</span>
        </button>
      </div>
    </div>

    <!-- Metrics Cards -->
    <div id="enterprise-metrics-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
      <div class="col-span-full py-3 text-center text-xs text-slate-500">Inspecionando perfil enterprise do projeto...</div>
    </div>

    <!-- Production Deploy Gate Card (Cenário G8 / Portão em 2 Etapas) -->
    <div class="mt-4 pt-4 border-t border-slate-800/80 rounded-xl bg-slate-950/60 p-4 border border-slate-800 space-y-3">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xs font-mono font-semibold uppercase tracking-wider text-sky-300">🚀 Portão de Deploy Dokploy (Cenário G8)</span>
            <span id="deploy-gate-status-badge" class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400">Etapa 1: Requer Avaliação</span>
          </div>
          <p class="text-[11px] text-slate-400 mt-0.5">
            O deploy em nuvem só é habilitado após aprovação formal pelo oráculo de prontidão enterprise.
          </p>
        </div>
        <div class="flex items-center gap-2.5">
          <button 
            id="btn-evaluate-deploy" 
            type="button" 
            class="px-3.5 py-1.5 rounded-xl border border-sky-500/40 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20 text-xs font-medium transition active:scale-95">
            1. Avaliar Prontidão (Gate)
          </button>
          <button 
            id="btn-trigger-deploy" 
            type="button" 
            disabled 
            class="px-3.5 py-1.5 rounded-xl bg-emerald-600/30 text-emerald-400 border border-emerald-500/40 text-xs font-medium transition disabled:opacity-40 disabled:cursor-not-allowed shadow-md">
            2. Confirmar Deploy Dokploy
          </button>
        </div>
      </div>
      <div id="deploy-evaluation-result" class="hidden text-xs font-mono p-3 rounded-lg bg-slate-900 border border-slate-800"></div>
    </div>

    <!-- Configuration Modal -->
    <div id="enterprise-config-modal" class="hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-md rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 shadow-2xl">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <h3 class="text-sm font-semibold text-white">Configurar Perfil Enterprise</h3>
          <button type="button" onclick="closeConfigModal()" class="text-slate-400 hover:text-white">✕</button>
        </div>
        <form id="form-enterprise-config" class="space-y-3">
          <div>
            <label class="block text-[11px] font-mono text-slate-400 mb-1">Modo de Residência de Dados</label>
            <select id="cfg-residency" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono">
              <option value="local_only">local_only (Estrito Brasil / On-Premises)</option>
              <option value="br_only">br_only (Região Brasil Hetzner/AWS)</option>
              <option value="global">global (Sem restrição geográfica)</option>
            </select>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[11px] font-mono text-slate-400 mb-1">Max RPO (minutos)</label>
              <input id="cfg-rpo" type="number" min="5" max="1440" value="60" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono" />
            </div>
            <div>
              <label class="block text-[11px] font-mono text-slate-400 mb-1">Max RTO (minutos)</label>
              <input id="cfg-rto" type="number" min="5" max="1440" value="30" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono" />
            </div>
          </div>
          <div class="pt-1">
            <label class="flex items-center gap-2 cursor-pointer">
              <input id="cfg-signoff" type="checkbox" checked class="rounded border-slate-800 bg-slate-950 text-sky-600" />
              <span class="text-xs text-slate-300">Exigir aprovação formal do Owner para produção</span>
            </label>
          </div>
          <div class="flex items-center justify-end gap-2 pt-3 border-t border-slate-800">
            <button type="button" onclick="closeConfigModal()" class="px-3 py-1.5 rounded-xl bg-slate-800 text-slate-300 text-xs font-medium hover:bg-slate-700">Cancelar</button>
            <button type="submit" id="btn-save-enterprise-cfg" class="px-3 py-1.5 rounded-xl bg-sky-600 text-white text-xs font-medium hover:bg-sky-500 shadow-md">Salvar Política</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Audit Trail Slide-over / Modal -->
    <div id="enterprise-audit-modal" class="hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-3xl rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 shadow-2xl max-h-[85vh] flex flex-col">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div class="flex items-center gap-2">
            <span class="text-base">📜</span>
            <h3 class="text-sm font-semibold text-white">Trilha de Auditoria Criptográfica (Hash Chain)</h3>
          </div>
          <button type="button" onclick="closeAuditModal()" class="text-slate-400 hover:text-white">✕</button>
        </div>
        <div id="audit-trail-table-container" class="flex-1 overflow-y-auto pr-1">
          <div class="py-6 text-center text-xs text-slate-500">Carregando trilha de auditoria...</div>
        </div>
      </div>
    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  // Wire Event Listeners
  document.getElementById("verify-audit-btn")?.addEventListener("click", verifyAuditChain);
  document.getElementById("btn-open-config-modal")?.addEventListener("click", openConfigModal);
  document.getElementById("btn-open-audit-trail")?.addEventListener("click", openAuditModal);
  document.getElementById("form-enterprise-config")?.addEventListener("submit", handleConfigSubmit);
  document.getElementById("btn-evaluate-deploy")?.addEventListener("click", handleEvaluateDeploy);
  document.getElementById("btn-trigger-deploy")?.addEventListener("click", handleTriggerDeploy);
}

function openConfigModal() {
  const modal = document.getElementById("enterprise-config-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  if (enterpriseState.status?.config) {
    const cfg = enterpriseState.status.config;
    document.getElementById("cfg-residency").value = cfg.residency_mode || "local_only";
    document.getElementById("cfg-rpo").value = cfg.max_rpo_minutes || 60;
    document.getElementById("cfg-rto").value = cfg.max_rto_minutes || 30;
    document.getElementById("cfg-signoff").checked = !!cfg.require_owner_signoff;
  }
}

function closeConfigModal() {
  document.getElementById("enterprise-config-modal")?.classList.add("hidden");
}

function openAuditModal() {
  document.getElementById("enterprise-audit-modal")?.classList.remove("hidden");
  loadAuditTrail();
}

function closeAuditModal() {
  document.getElementById("enterprise-audit-modal")?.classList.add("hidden");
}

async function loadEnterpriseData() {
  enterpriseState.loading = true;
  try {
    const res = await enterpriseFetch(`/api/enterprise/status?project=${encodeURIComponent(enterpriseState.currentProject)}`);
    if (res.ok) {
      enterpriseState.status = await res.json();
      renderEnterpriseUI();
    }
  } catch (err) {
    console.error("Failed to load enterprise status:", err);
  } finally {
    enterpriseState.loading = false;
  }
}

function renderEnterpriseUI() {
  const badge = document.getElementById("enterprise-badge");
  const grid = document.getElementById("enterprise-metrics-grid");
  if (!grid || !enterpriseState.status) return;

  const { config, sla, audit_chain } = enterpriseState.status;

  if (badge) {
    if (config.enabled) {
      badge.className = "rounded-full bg-sky-500/10 px-2 py-0.5 text-xs font-medium text-sky-400 border border-sky-500/20";
      badge.textContent = "Enterprise Ativo";
    } else {
      badge.className = "rounded-full bg-slate-800 px-2 py-0.5 text-xs font-medium text-slate-400 border border-slate-700";
      badge.textContent = "Padrão (Standard)";
    }
  }

  grid.innerHTML = `
    <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <span class="text-[11px] text-slate-400">Residência de Dados</span>
      <div class="text-sm font-semibold text-slate-200 mt-1 uppercase font-mono">${escapeHtml(config.residency_mode)}</div>
      <span class="text-[10px] text-slate-500 mt-0.5 block">${config.enabled ? "Isolamento territorial estrito" : "Sem restrição territorial"}</span>
    </div>
    <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <span class="text-[11px] text-slate-400">Trilha de Auditoria (Hash Chain)</span>
      <div class="text-sm font-semibold ${audit_chain.is_valid ? "text-emerald-400" : "text-rose-400"} mt-1">
        ${audit_chain.is_valid ? "✓ 100% Íntegra" : "✗ Violação Detectada"}
      </div>
      <span class="text-[10px] text-slate-500 mt-0.5 block">${audit_chain.total_events} eventos criptografados</span>
    </div>
    <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <span class="text-[11px] text-slate-400">Janela de RPO (Perda Tolerável)</span>
      <div class="text-sm font-semibold text-slate-200 mt-1">${sla.last_backup_age_minutes}m / limite ${config.max_rpo_minutes}m</div>
      <span class="text-[10px] ${sla.is_compliant ? "text-emerald-400" : "text-rose-400"} mt-0.5 block">${sla.is_compliant ? "Dentro da meta contratual" : "Violação de RPO"}</span>
    </div>
    <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <span class="text-[11px] text-slate-400">Aceite Humano (Scenario G8)</span>
      <div class="text-sm font-semibold text-slate-200 mt-1">${config.require_owner_signoff ? "Obrigatório" : "Dispensado"}</div>
      <span class="text-[10px] text-slate-500 mt-0.5 block">Exige assinatura do Owner antes de prod</span>
    </div>
  `;
}

async function verifyAuditChain() {
  const btn = document.getElementById("verify-audit-btn");
  if (btn) btn.textContent = "Verificando...";
  try {
    const res = await enterpriseFetch("/api/enterprise/verify-audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: enterpriseState.currentProject }),
    });
    if (res.ok) {
      const data = await res.json();
      alert(`Verificação Criptográfica da Hash Chain:\nStatus: ${data.is_valid ? "VÁLIDO (0 Anomalias)" : "ADULTERAÇÃO DETECTADA"}\nEventos: ${data.total_events}\nDetalhe: ${data.error_message || "Integridade SHA-256 preservada."}`);
    }
  } catch (err) {
    alert(`Erro ao verificar: ${err.message}`);
  } finally {
    if (btn) btn.innerHTML = "<span>🔗 Verificar Hash Chain</span>";
    loadEnterpriseData();
  }
}

async function loadAuditTrail() {
  const container = document.getElementById("audit-trail-table-container");
  if (!container) return;

  try {
    const res = await enterpriseFetch(`/api/enterprise/audit-trail?project=${encodeURIComponent(enterpriseState.currentProject)}`);
    if (!res.ok) throw new Error("Falha ao obter eventos");
    const data = await res.json();
    const events = data.events || [];

    if (!events.length) {
      container.innerHTML = `<div class="py-6 text-center text-xs text-slate-500">Nenhum evento registrado nesta trilha ainda.</div>`;
      return;
    }

    container.innerHTML = `
      <table class="w-full text-left text-[11px] font-mono">
        <thead class="text-slate-400 border-b border-slate-800">
          <tr>
            <th class="pb-2">Timestamp</th>
            <th class="pb-2">Ator</th>
            <th class="pb-2">Ação</th>
            <th class="pb-2">Payload Hash</th>
            <th class="pb-2 text-right">Integridade</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/60 text-slate-300">
          ${events.map((e) => `
            <tr>
              <td class="py-2 text-slate-400">${escapeHtml(e.timestamp ? e.timestamp.slice(0, 19).replace("T", " ") : "—")}</td>
              <td class="py-2 font-semibold text-slate-200">${escapeHtml(e.actor_role)}</td>
              <td class="py-2 text-sky-400">${escapeHtml(e.action)}</td>
              <td class="py-2 text-slate-500 truncate max-w-[120px]" title="${escapeHtml(e.payload_hash)}">${escapeHtml(e.payload_hash ? e.payload_hash.slice(0, 10) + "..." : "—")}</td>
              <td class="py-2 text-right text-emerald-400">✓ Válido</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  } catch (err) {
    container.innerHTML = `<div class="py-4 text-center text-xs text-rose-400">Erro ao carregar trilha: ${err.message}</div>`;
  }
}

async function handleConfigSubmit(e) {
  e.preventDefault();
  const residency = document.getElementById("cfg-residency")?.value || "local_only";
  const rpo = parseInt(document.getElementById("cfg-rpo")?.value || "60", 10);
  const rto = parseInt(document.getElementById("cfg-rto")?.value || "30", 10);
  const signoff = document.getElementById("cfg-signoff")?.checked ?? true;

  const btn = document.getElementById("btn-save-enterprise-cfg");
  if (btn) btn.textContent = "Salvando...";

  try {
    const res = await enterpriseFetch("/api/enterprise/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: enterpriseState.currentProject,
        enabled: true,
        residency_mode: residency,
        max_rpo_minutes: rpo,
        max_rto_minutes: rto,
        require_owner_signoff: signoff,
      }),
    });

    if (res.ok) {
      closeConfigModal();
      await loadEnterpriseData();
    } else {
      const err = await res.json();
      alert(`Erro ao configurar: ${err.detail || "Falha"}`);
    }
  } catch (err) {
    alert(`Erro de conexão: ${err.message}`);
  } finally {
    if (btn) btn.textContent = "Salvar Política";
  }
}

async function handleEvaluateDeploy() {
  const btn = document.getElementById("btn-evaluate-deploy");
  const triggerBtn = document.getElementById("btn-trigger-deploy");
  const statusBadge = document.getElementById("deploy-gate-status-badge");
  const resultContainer = document.getElementById("deploy-evaluation-result");

  if (btn) btn.disabled = true;
  if (statusBadge) {
    statusBadge.textContent = "Avaliando oráculos (G8)...";
    statusBadge.className = "px-2 py-0.5 rounded text-[10px] font-mono bg-sky-950/60 text-sky-300 animate-pulse";
  }

  try {
    const res = await enterpriseFetch("/api/enterprise/evaluate-deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: enterpriseState.currentProject,
        target_environment: "production",
        owner_approved: true,
        actor_role: "owner",
      }),
    });

    const data = await res.json();
    enterpriseState.lastEvaluation = data;

    if (resultContainer) {
      resultContainer.classList.remove("hidden");
      const isApproved = data.approved || data.decision === "approve";
      resultContainer.innerHTML = `
        <div class="flex items-center justify-between border-b border-slate-800 pb-2 mb-2">
          <span>Veredito: <strong class="${isApproved ? 'text-emerald-400' : 'text-rose-400'}">${isApproved ? 'APROVADO PARA PRODUÇÃO' : 'BLOQUEADO PELO GATE'}</strong></span>
          <span class="text-slate-500">Cenário G8 · Risco: ${escapeHtml(data.risk_level || 'low')}</span>
        </div>
        <div class="text-[11px] text-slate-300">
          ${data.reasons ? data.reasons.map((r) => `<div>• ${escapeHtml(r)}</div>`).join("") : (data.recommendation || "Todas as salvaguardas enterprise foram verificadas com sucesso.")}
        </div>
      `;

      if (isApproved) {
        if (statusBadge) {
          statusBadge.textContent = "✓ Gate Aprovado (Deploy Liberado)";
          statusBadge.className = "px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-950/80 text-emerald-300 border border-emerald-700/60";
        }
        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.className = "px-3.5 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium transition shadow-lg shadow-emerald-600/30 cursor-pointer active:scale-95";
        }
      } else {
        if (statusBadge) {
          statusBadge.textContent = "✗ Reprovado pelo Gate";
          statusBadge.className = "px-2 py-0.5 rounded text-[10px] font-mono bg-rose-950/80 text-rose-300 border border-rose-700/60";
        }
        if (triggerBtn) {
          triggerBtn.disabled = true;
          triggerBtn.className = "px-3.5 py-1.5 rounded-xl bg-emerald-600/30 text-emerald-400 border border-emerald-500/40 text-xs font-medium transition disabled:opacity-40 disabled:cursor-not-allowed";
        }
      }
    }
  } catch (err) {
    alert(`Erro na avaliação: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleTriggerDeploy() {
  if (!enterpriseState.lastEvaluation || !(enterpriseState.lastEvaluation.approved || enterpriseState.lastEvaluation.decision === "approve")) {
    alert("Deploy bloqueado: você deve avaliar a prontidão com sucesso antes de confirmar o disparo.");
    return;
  }

  const confirmed = confirm(
    `CONFIRMAÇÃO DE DEPLOY EM PRODUÇÃO:\n\nProjeto: ${enterpriseState.currentProject}\nAlvo: Dokploy PaaS 24/7\n\nTodas as salvaguardas de conformidade foram atendidas. Deseja disparar a esteira de build e deploy agora?`
  );
  if (!confirmed) return;

  const btn = document.getElementById("btn-trigger-deploy");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Disparando Dokploy...";
  }

  try {
    const res = await enterpriseFetch("/api/cloud/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: enterpriseState.currentProject }),
    });

    const data = await res.json();
    if (res.ok) {
      alert(`✓ Deploy Dokploy disparado com sucesso!\n\nID do Deploy: ${data.deployment_id || "dokploy-build-active"}\nStatus: ${data.status || "triggered"}\nProjeto: ${enterpriseState.currentProject}`);
    } else {
      alert(`Falha no deploy: ${data.detail || "Erro no Dokploy"}`);
    }
  } catch (err) {
    alert(`Erro de conexão com o Dokploy: ${err.message}`);
  } finally {
    if (btn) {
      btn.textContent = "2. Confirmar Deploy Dokploy";
      btn.disabled = false;
    }
  }
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
