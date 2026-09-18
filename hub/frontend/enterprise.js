/**
 * Enterprise Security & Compliance Monitor for DarkHub (HF-24).
 * Observes data residency, cryptographic audit chain integrity, and SLA RPO/RTO metrics.
 */

const enterpriseState = {
  currentProject: "darkfac",
  status: null,
  loading: false,
};

function initEnterprise() {
  mountEnterpriseSection();
  const select = document.getElementById("global-project-select");
  if (select) {
    enterpriseState.currentProject = select.value || "darkfac";
    select.addEventListener("change", (e) => {
      enterpriseState.currentProject = e.target.value;
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
    document.getElementById("cross-catalog-section") ||
    document.getElementById("infrastructure-cards-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "enterprise-security-section";
  section.className =
    "space-y-4 rounded-2xl border border-sky-900/40 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-sky-950/10";
  section.innerHTML = `
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-sky-500/10 text-sky-400 text-sm">🛡️</span>
          <h2 class="text-base font-semibold text-slate-100">Segurança Enterprise & Residência de Dados (HF-24)</h2>
          <span id="enterprise-badge" class="rounded-full bg-slate-800 px-2 py-0.5 text-xs font-medium text-slate-400 border border-slate-700">Auditando...</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">Isolamento reforçado, trilha de auditoria criptográfica SHA-256 e conformidade SLA para clientes pagantes.</p>
      </div>
      <div class="flex items-center gap-2">
        <button id="verify-audit-btn" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>🔗 Verificar Hash Chain</span>
        </button>
      </div>
    </div>
    <div id="enterprise-metrics-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
      <div class="col-span-full py-3 text-center text-xs text-slate-500">Inspecionando perfil enterprise do projeto...</div>
    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  const btn = document.getElementById("verify-audit-btn");
  if (btn) {
    btn.addEventListener("click", verifyAuditChain);
  }
}

async function loadEnterpriseData() {
  enterpriseState.loading = true;
  try {
    const res = await fetch(`/api/enterprise/status?project=${encodeURIComponent(enterpriseState.currentProject)}`);
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
      <span class="text-[10px] text-slate-500 mt-0.5 block">${config.enabled ? "Isolamento estrito ativo" : "Sem restrição territorial"}</span>
    </div>
    <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      <span class="text-[11px] text-slate-400">Trilha de Auditoria (Hash Chain)</span>
      <div class="text-sm font-semibold ${audit_chain.is_valid ? "text-emerald-400" : "text-rose-400"} mt-1">
        ${audit_chain.is_valid ? "✓ 100% Íntegra" : "✗ Violação Detectada"}
      </div>
      <span class="text-[10px] text-slate-500 mt-0.5 block">${audit_chain.total_events} eventos matematicamente encadeados</span>
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
    const res = await fetch("/api/enterprise/verify-audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: enterpriseState.currentProject }),
    });
    if (res.ok) {
      const data = await res.json();
      alert(`Verificação Criptográfica da Hash Chain:\nStatus: ${data.is_valid ? "VÁLIDO (0 Anomalias)" : "ADULTERAÇÃO DETECTADA"}\nEventos: ${data.total_events}\nDetalhe: ${data.error_message}`);
    }
  } catch (err) {
    alert(`Erro ao verificar: ${err}`);
  } finally {
    if (btn) btn.innerHTML = "<span>🔗 Verificar Hash Chain</span>";
    loadEnterpriseData();
  }
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
