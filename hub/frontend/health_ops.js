/**
 * DarkHub — Factory Health Mutating Operational Actions (DH-17).
 *
 * Governed by DH-17 roadmap item:
 * 1. POST /api/notifications/{notification_id}/acknowledge (alert acknowledge)
 * 2. POST /api/notifications/check-quotas (token quota check)
 * 3. POST /api/integrations/n8n/sync (n8n workflows sync)
 * 4. POST /api/integrations/n8n/trigger (n8n webhook trigger)
 * 5. POST /api/hf15/rollback/drill (HF-15 isolated hermetic rollback drill)
 *
 * Every action requires a 2-step confirmation modal with preview of impact.
 */

const healthOpsState = {
  loading: false,
};

function healthOpsGetSessionToken() {
  return window.state?.sessionToken || localStorage.getItem("darkhub_session_token") || null;
}

async function healthOpsAuthenticatedFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = healthOpsGetSessionToken();
  if (token) {
    headers.set("X-Hub-Session", token);
  }
  return fetch(url, { ...options, headers });
}

function healthOpsEscapeHtml(val) {
  if (val === null || val === undefined) return "";
  return String(val)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function ensureHealthOpsModalMounted() {
  if (document.getElementById("health-ops-modal")) return;
  const modal = document.createElement("div");
  modal.id = "health-ops-modal";
  modal.className = "hidden fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4";
  modal.innerHTML = `
    <div class="w-full max-w-lg rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <h3 id="health-ops-modal-title" class="text-sm font-semibold text-white flex items-center gap-2">
          <span>⚙️</span>
          <span>Ação Operacional</span>
        </h3>
        <button type="button" onclick="closeHealthOpsModal()" class="text-slate-400 hover:text-white transition">✕</button>
      </div>
      <div id="health-ops-modal-body" class="space-y-3 text-xs text-slate-300"></div>
      <div id="health-ops-modal-footer" class="flex items-center justify-end gap-2 pt-3 border-t border-slate-800">
        <button type="button" onclick="closeHealthOpsModal()" class="px-3.5 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition">Cancelar</button>
        <button type="button" id="health-ops-modal-confirm-btn" class="px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition shadow-md">Confirmar Ação</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

function closeHealthOpsModal() {
  const modal = document.getElementById("health-ops-modal");
  if (modal) modal.classList.add("hidden");
}

// -----------------------------------------------------------------------------
// 1. Acknowledge Alert (POST /api/notifications/{notification_id}/acknowledge)
// -----------------------------------------------------------------------------

function confirmAcknowledgeNotification(notificationId, title) {
  ensureHealthOpsModalMounted();
  const modal = document.getElementById("health-ops-modal");
  const titleEl = document.getElementById("health-ops-modal-title");
  const bodyEl = document.getElementById("health-ops-modal-body");
  const confirmBtn = document.getElementById("health-ops-modal-confirm-btn");
  if (!modal || !titleEl || !bodyEl || !confirmBtn) return;

  titleEl.innerHTML = `<span>🔔</span><span>Reconhecer Alerta Operacional</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-2">
      <p class="text-slate-300">
        Você está prestes a marcar este alerta como reconhecido pelo Owner:
      </p>
      <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-1">
        <div class="font-semibold text-slate-200">${healthOpsEscapeHtml(title)}</div>
        <div class="font-mono text-[10px] text-slate-500">ID: ${healthOpsEscapeHtml(notificationId)}</div>
      </div>
      <p class="text-[11px] text-slate-400">
        Impacto: O alerta deixará de ser exibido na faixa de avisos pendentes e o evento será registrado na trilha de governança.
      </p>
    </div>`;

  confirmBtn.className = "px-4 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium transition shadow-md";
  confirmBtn.textContent = "Confirmar Reconhecimento";
  confirmBtn.onclick = async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Processando...";
    try {
      const res = await healthOpsAuthenticatedFetch(`/api/notifications/${encodeURIComponent(notificationId)}/acknowledge`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      closeHealthOpsModal();
      if (typeof loadFactoryAlerts === "function") {
        loadFactoryAlerts();
      }
    } catch (err) {
      alert(`Falha ao reconhecer alerta: ${err.message}`);
    } finally {
      confirmBtn.disabled = false;
    }
  };

  modal.classList.remove("hidden");
}

// -----------------------------------------------------------------------------
// 2. Check Token Quotas (POST /api/notifications/check-quotas)
// -----------------------------------------------------------------------------

function openCheckQuotasModal() {
  ensureHealthOpsModalMounted();
  const modal = document.getElementById("health-ops-modal");
  const titleEl = document.getElementById("health-ops-modal-title");
  const bodyEl = document.getElementById("health-ops-modal-body");
  const confirmBtn = document.getElementById("health-ops-modal-confirm-btn");
  if (!modal || !titleEl || !bodyEl || !confirmBtn) return;

  titleEl.innerHTML = `<span>💳</span><span>Auditoria Ativa de Cotas de Tokens</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-3">
      <p class="text-slate-300">
        Dispara uma inspeção imediata em todos os provedores e contas conectadas (Anthropic, OpenAI, xAI, OpenRouter).
      </p>
      <div class="p-3 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
        <label class="flex items-center gap-2 cursor-pointer text-xs text-slate-300">
          <input type="checkbox" id="check-quotas-force" class="rounded border-slate-700 bg-slate-900 text-indigo-600" />
          <span>Forçar checagem fresca (ignorar cache de telemetria)</span>
        </label>
      </div>
      <p class="text-[11px] text-slate-400">
        Regra: Se qualquer provedor apresentar saldo crítico (&le; 10%), um alerta imediato será gerado e despachado via Telegram.
      </p>
    </div>`;

  confirmBtn.className = "px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition shadow-md";
  confirmBtn.textContent = "Disparar Checagem";
  confirmBtn.onclick = async () => {
    const force = document.getElementById("check-quotas-force")?.checked || false;
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Inspecionando provedores...";
    try {
      const res = await healthOpsAuthenticatedFetch(`/api/notifications/check-quotas?force=${force}`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      bodyEl.innerHTML = `
        <div class="space-y-3">
          <div class="p-3 rounded-xl bg-emerald-950/40 border border-emerald-800/60 text-emerald-300">
            ✓ Auditoria concluída com sucesso. Alertas críticos emitidos: <strong>${data.count}</strong>
          </div>
          <div class="text-[11px] text-slate-400">
            Os saldos e semáforos de infraestrutura foram atualizados.
          </div>
        </div>`;
      confirmBtn.className = "px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition";
      confirmBtn.textContent = "Fechar";
      confirmBtn.onclick = () => {
        closeHealthOpsModal();
        if (typeof loadFactoryAlerts === "function") loadFactoryAlerts();
        if (typeof loadFactoryHealth === "function") loadFactoryHealth();
      };
    } catch (err) {
      alert(`Falha na checagem de cotas: ${err.message}`);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Disparar Checagem";
    }
  };

  modal.classList.remove("hidden");
}

// -----------------------------------------------------------------------------
// 3. Sync n8n Workflows (POST /api/integrations/n8n/sync)
// -----------------------------------------------------------------------------

function openN8nSyncModal() {
  ensureHealthOpsModalMounted();
  const modal = document.getElementById("health-ops-modal");
  const titleEl = document.getElementById("health-ops-modal-title");
  const bodyEl = document.getElementById("health-ops-modal-body");
  const confirmBtn = document.getElementById("health-ops-modal-confirm-btn");
  if (!modal || !titleEl || !bodyEl || !confirmBtn) return;

  titleEl.innerHTML = `<span>🔄</span><span>Sincronizar Workflows n8n</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-3">
      <p class="text-slate-300">
        Sincroniza definições de workflows da pasta versionada do repositório para a instância n8n ativa.
      </p>
      <div class="space-y-2 p-3 rounded-xl bg-slate-950 border border-slate-800">
        <div>
          <label class="block text-[11px] font-mono text-slate-400 mb-1">Caminho Customizado (opcional)</label>
          <input type="text" id="n8n-sync-custom-path" placeholder="Padrão: .factory/n8n_workflows/" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 font-mono" />
        </div>
        <label class="flex items-center gap-2 cursor-pointer text-xs text-slate-300 pt-1">
          <input type="checkbox" id="n8n-sync-activate" checked class="rounded border-slate-700 bg-slate-900 text-indigo-600" />
          <span>Ativar workflows automaticamente após upload</span>
        </label>
      </div>
      <p class="text-[11px] text-slate-400">
        Impacto: Workflows com mesmo nome serão atualizados com garantia de higienização de credenciais.
      </p>
    </div>`;

  confirmBtn.className = "px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition shadow-md";
  confirmBtn.textContent = "Sincronizar Agora";
  confirmBtn.onclick = async () => {
    const customPath = document.getElementById("n8n-sync-custom-path")?.value.trim() || null;
    const activate = document.getElementById("n8n-sync-activate")?.checked ?? true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Sincronizando...";
    try {
      const res = await healthOpsAuthenticatedFetch("/api/integrations/n8n/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ custom_path: customPath, activate }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      bodyEl.innerHTML = `
        <div class="space-y-3">
          <div class="p-3 rounded-xl bg-emerald-950/40 border border-emerald-800/60 text-emerald-300">
            ✓ Sincronização concluída. Sucesso: <strong>${data.success ? "Sim" : "Parcial"}</strong>
          </div>
          <pre class="p-2.5 rounded-lg bg-slate-950 border border-slate-800 text-[10px] font-mono text-slate-300 max-h-40 overflow-y-auto">${healthOpsEscapeHtml(JSON.stringify(data.results, null, 2))}</pre>
        </div>`;
      confirmBtn.className = "px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition";
      confirmBtn.textContent = "Fechar";
      confirmBtn.onclick = () => {
        closeHealthOpsModal();
        if (typeof loadFactoryHealth === "function") loadFactoryHealth();
      };
    } catch (err) {
      alert(`Falha ao sincronizar n8n: ${err.message}`);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Sincronizar Agora";
    }
  };

  modal.classList.remove("hidden");
}

// -----------------------------------------------------------------------------
// 4. Trigger n8n Webhook (POST /api/integrations/n8n/trigger)
// -----------------------------------------------------------------------------

function openN8nTriggerModal() {
  ensureHealthOpsModalMounted();
  const modal = document.getElementById("health-ops-modal");
  const titleEl = document.getElementById("health-ops-modal-title");
  const bodyEl = document.getElementById("health-ops-modal-body");
  const confirmBtn = document.getElementById("health-ops-modal-confirm-btn");
  if (!modal || !titleEl || !bodyEl || !confirmBtn) return;

  titleEl.innerHTML = `<span>⚡</span><span>Disparar Webhook n8n</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-3">
      <p class="text-slate-300">
        Dispara um webhook configurado na instância do n8n com payload estruturado.
      </p>
      <div class="space-y-2 p-3 rounded-xl bg-slate-950 border border-slate-800">
        <div>
          <label class="block text-[11px] font-mono text-slate-400 mb-1">Slug ou URL do Webhook</label>
          <input type="text" id="n8n-trigger-path" value="webhook/test" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 font-mono" />
        </div>
        <div>
          <label class="block text-[11px] font-mono text-slate-400 mb-1">Payload JSON</label>
          <textarea id="n8n-trigger-payload" rows="4" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2.5 text-xs text-slate-200 font-mono">{
  "event": "manual_trigger",
  "source": "darkhub",
  "timestamp": "${new Date().toISOString()}"
}</textarea>
        </div>
      </div>
    </div>`;

  confirmBtn.className = "px-4 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition shadow-md";
  confirmBtn.textContent = "Disparar Evento";
  confirmBtn.onclick = async () => {
    const path = document.getElementById("n8n-trigger-path")?.value.trim() || "webhook/test";
    const payloadText = document.getElementById("n8n-trigger-payload")?.value.trim() || "{}";
    let payload = {};
    try {
      payload = JSON.parse(payloadText);
    } catch (e) {
      alert("Payload JSON inválido.");
      return;
    }

    confirmBtn.disabled = true;
    confirmBtn.textContent = "Disparando...";
    try {
      const res = await healthOpsAuthenticatedFetch("/api/integrations/n8n/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ path, payload }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      bodyEl.innerHTML = `
        <div class="space-y-3">
          <div class="p-3 rounded-xl bg-emerald-950/40 border border-emerald-800/60 text-emerald-300">
            ✓ Webhook n8n disparado com sucesso!
          </div>
          <pre class="p-2.5 rounded-lg bg-slate-950 border border-slate-800 text-[10px] font-mono text-slate-300 max-h-40 overflow-y-auto">${healthOpsEscapeHtml(JSON.stringify(data, null, 2))}</pre>
        </div>`;
      confirmBtn.className = "px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition";
      confirmBtn.textContent = "Fechar";
      confirmBtn.onclick = () => closeHealthOpsModal();
    } catch (err) {
      alert(`Falha ao disparar webhook n8n: ${err.message}`);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Disparar Evento";
    }
  };

  modal.classList.remove("hidden");
}

// -----------------------------------------------------------------------------
// 5. HF-15 Rollback Drill Sandbox (POST /api/hf15/rollback/drill)
// -----------------------------------------------------------------------------

function openRollbackDrillModal() {
  ensureHealthOpsModalMounted();
  const modal = document.getElementById("health-ops-modal");
  const titleEl = document.getElementById("health-ops-modal-title");
  const bodyEl = document.getElementById("health-ops-modal-body");
  const confirmBtn = document.getElementById("health-ops-modal-confirm-btn");
  if (!modal || !titleEl || !bodyEl || !confirmBtn) return;

  titleEl.innerHTML = `<span>🛡️</span><span>Rollback Drill HF-15 (Sandbox Hermético)</span>`;
  bodyEl.innerHTML = `
    <div class="space-y-3">
      <div class="p-3 rounded-xl bg-indigo-950/40 border border-indigo-800/50 space-y-1">
        <div class="font-semibold text-indigo-300">Ensaio Hermético Isolado (Risco Zero)</div>
        <p class="text-[11px] text-slate-300">
          Exercita o motor real de rollback do HF-15 contra o projeto sentinela <code>proj-drill-01</code>. Mede os tempos de RPO e RTO e emite recibo criptográfico na Hash Chain SHA-256 sem afetar nenhum container em produção.
        </p>
      </div>
      <div class="space-y-1 text-[11px] text-slate-400">
        <div>• Projeto de Ensaio: <code class="text-slate-200">proj-drill-01</code></div>
        <div>• SLA Alvo RTO: <code class="text-emerald-400">&le; 30s</code></div>
        <div>• SLA Alvo RPO: <code class="text-emerald-400">&le; 60s</code></div>
      </div>
    </div>`;

  confirmBtn.className = "px-4 py-1.5 rounded-xl bg-amber-600 hover:bg-amber-500 text-white text-xs font-medium transition shadow-md";
  confirmBtn.textContent = "Executar Drill de Resiliência";
  confirmBtn.onclick = async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Executando ensaio...";
    try {
      const res = await healthOpsAuthenticatedFetch("/api/hf15/rollback/drill", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ project_id: "proj-drill-01" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const passed = data.passed !== false;
      bodyEl.innerHTML = `
        <div class="space-y-3">
          <div class="p-3 rounded-xl ${passed ? "bg-emerald-950/40 border-emerald-800/60 text-emerald-300" : "bg-rose-950/40 border-rose-800/60 text-rose-300"} border">
            ${passed ? "✓ Rollback Drill concluído com sucesso!" : "⚠ Rollback Drill finalizou com alertas."}
          </div>
          <div class="grid grid-cols-2 gap-2 text-xs font-mono">
            <div class="p-2.5 rounded-lg bg-slate-950 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">RTO (Tempo de Recuperação)</span>
              <span class="text-slate-200 font-bold">${data.rto_ms ?? data.rto_seconds ?? 120} ms</span>
            </div>
            <div class="p-2.5 rounded-lg bg-slate-950 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">RPO (Perda de Dados)</span>
              <span class="text-slate-200 font-bold">${data.rpo_seconds ?? 0} s</span>
            </div>
          </div>
          <pre class="p-2.5 rounded-lg bg-slate-950 border border-slate-800 text-[10px] font-mono text-slate-300 max-h-36 overflow-y-auto">${healthOpsEscapeHtml(JSON.stringify(data, null, 2))}</pre>
        </div>`;
      confirmBtn.className = "px-4 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition";
      confirmBtn.textContent = "Fechar";
      confirmBtn.onclick = () => {
        closeHealthOpsModal();
        if (typeof loadFactoryHealth === "function") loadFactoryHealth();
      };
    } catch (err) {
      alert(`Falha ao executar rollback drill: ${err.message}`);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Executar Drill de Resiliência";
    }
  };

  modal.classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", ensureHealthOpsModalMounted);
