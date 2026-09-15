/**
 * Infrastructure & Topology Cards Monitor for DarkHub.
 * Governed by USR-15: Read-only visualization of hybrid infrastructure,
 * PCs, Hetzner VPS, Tailscale mesh, Docker, Dokploy and official access links.
 */

const infraState = {
  report: null,
  loading: false,
};

function initInfra() {
  mountInfraMonitor();
  loadInfraCards(false);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initInfra);
} else {
  initInfra();
}

function mountInfraMonitor() {
  if (document.getElementById("infrastructure-cards-section")) return;
  const targetAnchor =
    document.getElementById("api-credits-section") ||
    document.getElementById("ai-usage-section") ||
    document.getElementById("quick-dock-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "infrastructure-cards-section";
  section.className =
    "space-y-4 rounded-2xl border border-indigo-900/40 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-indigo-950/10";
  section.innerHTML = `
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-400 text-sm">🖥️</span>
          <h2 class="text-sm font-semibold text-white">Infraestrutura & Recursos de Rede</h2>
          <span id="infra-health-pill" class="rounded-full border border-indigo-700/60 bg-indigo-950/40 px-2.5 py-0.5 text-[10px] font-mono text-indigo-300">sincronizando</span>
        </div>
        <p class="mt-1 text-[11px] text-slate-400">
          Visualização consolidada de nós on-premises, Hetzner VPS, malha Tailscale, contêineres Docker/Dokploy e links de acesso direto.
        </p>
      </div>
      <div class="flex items-center gap-3">
        <div class="hidden sm:flex flex-col items-end text-right">
          <span class="text-[10px] uppercase font-mono text-slate-400">Custo Total Infra/Mês</span>
          <span id="infra-total-budget" class="text-xs font-bold font-mono text-indigo-300">$0.00/mês</span>
        </div>
        <button id="refresh-infra-cards" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-3 py-1.5 text-[11px] font-medium text-indigo-300 transition hover:bg-indigo-500/20 active:scale-95">
          <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Atualizar Infraestrutura
        </button>
      </div>
    </div>
    <div id="infra-cards-grid" class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      ${infraSkeleton(3)}
    </div>
    <div id="cloud-gateway-banner" class="mt-4 pt-3 border-t border-slate-800/80">
      <div class="h-16 animate-pulse rounded-xl bg-slate-950/60 border border-slate-800/60"></div>
    </div>`;

  if (targetAnchor.tagName && targetAnchor.tagName.toLowerCase() === "main") {
    targetAnchor.appendChild(section);
  } else {
    targetAnchor.insertAdjacentElement("afterend", section);
  }
  document.getElementById("refresh-infra-cards")?.addEventListener("click", () => loadInfraCards(true));
}

function infraSkeleton(count) {
  return Array.from({ length: count }, () => `
    <div class="h-48 animate-pulse rounded-xl border border-slate-800 bg-slate-950/60 p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between">
        <div class="h-4 w-28 rounded bg-slate-800"></div>
        <div class="h-4 w-14 rounded-full bg-slate-800"></div>
      </div>
      <div class="space-y-2">
        <div class="h-3 w-40 rounded bg-slate-800/80"></div>
        <div class="h-3 w-32 rounded bg-slate-800/60"></div>
      </div>
      <div class="h-8 rounded bg-slate-800/60"></div>
    </div>`).join("");
}

async function loadInfraCards(force = false) {
  if (infraState.loading) return;
  infraState.loading = true;
  const button = document.getElementById("refresh-infra-cards");
  if (button) {
    button.disabled = true;
    button.classList.add("opacity-60");
  }

  try {
    const activeProj = window.currentActiveProjectId || localStorage.getItem("darkhub_active_project") || "";
    let endpoint = force ? "/api/infra/cards/refresh" : "/api/infra/cards";
    if (activeProj) {
      endpoint += `?project_id=${encodeURIComponent(activeProj)}`;
    }
    const method = force ? "POST" : "GET";
    const response = await fetch(endpoint, { method });
    if (!response.ok) throw new Error(`Infra API error: ${response.status}`);
    infraState.report = await response.json();
    renderInfraCards();
  } catch (error) {
    console.warn("Falha ao carregar cards de infraestrutura:", error);
    renderInfraError(error.message);
  } finally {
    infraState.loading = false;
    if (button) {
      button.disabled = false;
      button.classList.remove("opacity-60");
    }
  }
}

function renderInfraCards() {
  const report = infraState.report;
  if (!report) return;

  const budgetEl = document.getElementById("infra-total-budget");
  if (budgetEl) {
    budgetEl.textContent = `$${report.total_monthly_budget_usd.toFixed(2)}/mês`;
  }

  const pill = document.getElementById("infra-health-pill");
  if (pill) {
    pill.className =
      "rounded-full border border-indigo-700/60 bg-indigo-950/40 px-2.5 py-0.5 text-[10px] font-mono text-indigo-300";
    pill.textContent = `${report.active_nodes}/${report.total_nodes} nós ativos`;
  }

  const container = document.getElementById("infra-cards-grid");
  if (!container) return;

  if (!report.cards || report.cards.length === 0) {
    container.innerHTML = `
      <div class="col-span-full py-8 text-center text-xs text-slate-500 font-mono">
        Nenhum nó de infraestrutura registrado no inventário.
      </div>`;
    return;
  }

  container.innerHTML = report.cards.map((card) => renderSingleInfraCard(card)).join("");
  renderCloudGatewayBanner();
}

function renderSingleInfraCard(card) {
  const statusColors = {
    active: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
    standby: "bg-amber-500/20 text-amber-300 border-amber-500/30",
    provisioning: "bg-cyan-500/20 text-cyan-300 border-cyan-500/30",
    planned: "bg-slate-800 text-slate-400 border-slate-700",
    offline: "bg-rose-500/20 text-rose-300 border-rose-500/30",
  };

  const roleColors = {
    cloud_vps: "text-cyan-400 bg-cyan-950/60 border-cyan-800/40",
    dev_workstation: "text-indigo-400 bg-indigo-950/60 border-indigo-800/40",
    on_prem_server: "text-purple-400 bg-purple-950/60 border-purple-800/40",
    edge_gateway: "text-amber-400 bg-amber-950/60 border-amber-800/40",
    managed_service: "text-blue-400 bg-blue-950/60 border-blue-800/40",
  };

  const statusClass = statusColors[card.status] || "bg-slate-800 text-slate-400 border-slate-700";
  const roleClass = roleColors[card.role] || "text-slate-300 bg-slate-800 border-slate-700";

  // Reachability badge
  let reachBadge = "";
  if (card.is_live_reachable === true) {
    reachBadge = `<span class="inline-flex items-center gap-1 text-[10px] font-mono text-emerald-400">
      <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span> online
    </span>`;
  } else if (card.is_live_reachable === false) {
    reachBadge = `<span class="inline-flex items-center gap-1 text-[10px] font-mono text-amber-400/80">
      <span class="h-1.5 w-1.5 rounded-full bg-amber-400"></span> unreach
    </span>`;
  }

  // Network entries
  const net = card.network_summary || {};
  let networkBadges = "";
  if (net.tailscale_ip) {
    networkBadges += `
      <div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-300">
        <span class="text-indigo-400 font-semibold">Tailscale:</span>
        <span class="bg-slate-900 px-1.5 py-0.5 rounded border border-slate-800 text-slate-200">${net.tailscale_ip}</span>
      </div>`;
  }
  if (net.public_ip) {
    networkBadges += `
      <div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-300">
        <span class="text-cyan-400 font-semibold">IPv4:</span>
        <span class="bg-slate-900 px-1.5 py-0.5 rounded border border-slate-800 text-slate-200">${net.public_ip}</span>
      </div>`;
  }
  if (net.public_dns) {
    networkBadges += `
      <div class="flex items-center gap-1.5 text-[11px] font-mono text-slate-300">
        <span class="text-emerald-400 font-semibold">FQDN:</span>
        <span class="bg-slate-900 px-1.5 py-0.5 rounded border border-slate-800 text-slate-200 truncate max-w-[200px]" title="${net.public_dns}">${net.public_dns}</span>
      </div>`;
  }

  // Services tags
  const servicesTags = (card.services || []).slice(0, 4).map((s) => `
    <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-900 border border-slate-800 text-slate-400 flex items-center gap-1" title="${s.description || s.name}">
      <span class="h-1 w-1 rounded-full ${s.status === 'active' ? 'bg-emerald-400' : 'bg-slate-500'}"></span>
      ${s.name}
    </span>`).join("");

  // System Action Links
  const linksHtml = (card.links || []).map((link) => {
    const isPrimary = link.is_primary;
    const baseBtnStyle = isPrimary
      ? "bg-indigo-600/90 hover:bg-indigo-500 text-white font-semibold shadow-sm shadow-indigo-600/20"
      : "bg-slate-800/80 hover:bg-slate-700 text-slate-300 hover:text-white";
    return `
      <a
        href="${link.url}"
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-slate-700/60 text-xs transition active:scale-95 ${baseBtnStyle}"
        title="Abrir ${link.title} (${link.url})"
      >
        <span>${link.title}</span>
        ${link.badge ? `<span class="text-[9px] px-1 py-0.2 rounded bg-black/30 font-mono opacity-80">${link.badge}</span>` : ""}
        <svg class="h-3 w-3 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
        </svg>
      </a>`;
  }).join("");

  return `
    <div id="infra-card-${card.id}" class="flex flex-col justify-between rounded-xl border border-slate-800/90 bg-slate-950/70 p-4 shadow-sm hover:border-slate-700 transition duration-150">
      <!-- Top Badges -->
      <div class="space-y-2">
        <div class="flex items-center justify-between gap-2 flex-wrap">
          <span class="px-2 py-0.5 rounded text-[10px] font-mono border font-medium ${roleClass}">
            ${card.role_label}
          </span>
          <div class="flex items-center gap-2">
            ${reachBadge}
            <span class="px-2 py-0.5 rounded-full text-[10px] font-mono border uppercase tracking-wider ${statusClass}">
              ${card.status}
            </span>
          </div>
        </div>

        <!-- Node Title & Provider -->
        <div>
          <h3 class="text-sm font-semibold text-white tracking-tight flex items-center gap-1.5">
            ${card.name}
          </h3>
          <p class="text-[11px] text-slate-400 font-mono">${card.provider}</p>
        </div>

        <!-- Hardware specs -->
        ${card.hardware_summary ? `
          <div class="text-[11px] text-slate-300 font-mono bg-slate-900/60 px-2 py-1.5 rounded border border-slate-800/80">
            ${card.hardware_summary}
          </div>` : ""}

        <!-- Network details -->
        ${networkBadges ? `<div class="space-y-1 pt-1">${networkBadges}</div>` : ""}

        <!-- Services -->
        ${servicesTags ? `
          <div class="flex items-center gap-1.5 flex-wrap pt-2 border-t border-slate-900">
            ${servicesTags}
          </div>` : ""}
      </div>

      <!-- Footer: Cost & Action Links -->
      <div class="pt-4 mt-3 border-t border-slate-800/80 flex flex-col gap-2.5">
        <div class="flex items-center justify-between text-[11px] font-mono">
          <span class="text-slate-500">Custo:</span>
          <span class="text-slate-300 font-semibold">$${card.cost_monthly_usd.toFixed(2)}/mês</span>
        </div>
        ${linksHtml ? `
          <div class="flex items-center gap-2 flex-wrap">
            ${linksHtml}
          </div>` : ""}
      </div>
    </div>`;
}

function renderInfraError(msg) {
  const pill = document.getElementById("infra-health-pill");
  if (pill) {
    pill.className =
      "rounded-full border border-rose-700/60 bg-rose-950/40 px-2.5 py-0.5 text-[10px] font-mono text-rose-300";
    pill.textContent = "falha na API";
  }
}

async function renderCloudGatewayBanner() {
  const banner = document.getElementById("cloud-gateway-banner");
  if (!banner) return;

  try {
    const res = await fetch("/api/cloud/status");
    if (!res.ok) return;
    const status = await res.json();

    const isCloud = status.is_cloud;
    const cloudBadge = isCloud
      ? '<span class="px-2 py-0.5 rounded-full text-[10px] font-mono bg-emerald-950/60 text-emerald-300 border border-emerald-700/60 font-medium">Dokploy PaaS 24/7 (Ativo)</span>'
      : '<span class="px-2 py-0.5 rounded-full text-[10px] font-mono bg-indigo-950/60 text-indigo-300 border border-indigo-700/60 font-medium">Local Workstation (Hybrid Edge)</span>';

    const cfBadge = status.cloudflare_zero_trust_enabled
      ? '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-amber-950/40 text-amber-300 border border-amber-800/60">Zero Trust WAF Ativo</span>'
      : '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800/80 text-slate-300 border border-slate-700/60">Traefik TLS / Dokploy</span>';

    const webhookStatus = status.webhook_secret_configured
      ? '<span class="text-emerald-400 font-medium">HMAC-SHA256 Ativo</span>'
      : '<span class="text-amber-400 font-medium">Modo Simulado / Dev</span>';

    banner.innerHTML = `
      <div class="rounded-xl border border-indigo-800/40 bg-slate-950/80 p-3.5 sm:p-4 flex flex-col md:flex-row items-start md:items-center justify-between gap-3 shadow-inner">
        <div class="space-y-1">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-xs font-semibold text-white flex items-center gap-1.5">
              <span class="h-2 w-2 rounded-full bg-emerald-400 animate-ping"></span>
              DarkHub 24/7 Gateway & Webhooks Dokploy
            </span>
            ${cloudBadge}
            ${cfBadge}
          </div>
          <div class="flex items-center gap-4 text-[11px] font-mono text-slate-400 flex-wrap">
            <span>Webhook Listener: ${webhookStatus}</span>
            <span>Entregas: <strong class="text-slate-200">${status.total_events_received}</strong></span>
            ${status.last_event_type ? `<span>Ultimo: <code class="text-cyan-300">${status.last_event_type}</code></span>` : ""}
            <span>Endpoint: <code class="text-slate-300">/api/webhooks/github</code></span>
          </div>
        </div>
        <div class="flex items-center gap-2 self-end md:self-auto">
          <button id="btn-test-webhook-ping" type="button" class="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-indigo-500/40 bg-indigo-600/20 text-indigo-300 text-xs hover:bg-indigo-600/30 transition active:scale-95">
            <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <span>Testar Webhook Ping</span>
          </button>
        </div>
      </div>`;

    document.getElementById("btn-test-webhook-ping")?.addEventListener("click", async () => {
      const btn = document.getElementById("btn-test-webhook-ping");
      if (btn) btn.disabled = true;
      try {
        const pingRes = await fetch("/api/webhooks/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            event_type: "ping",
            payload: { zen: "Autonomous software factories build the future.", hook_id: 101 },
          }),
        });
        if (pingRes.ok) {
          renderCloudGatewayBanner();
        }
      } catch (err) {
        console.warn("Falha ao testar webhook ping:", err);
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  } catch (err) {
    console.debug("Cloud Gateway probe:", err);
  }
}

