// DarkHub Capability Coverage & Governance Telemetry (USR-42 / DH-14)
// Exposes the factory coverage gate status, drift alerts, and pending roadmap breakdown.

let hubCoverageData = null;

async function loadHubCoverage(forceRefresh = false) {
  try {
    const url = forceRefresh ? '/api/hub/coverage?force_refresh=true' : '/api/hub/coverage';
    const res = await fetch(url);
    if (!res.ok) {
      console.warn('Falha ao carregar cobertura do DarkHub:', res.status);
      return;
    }
    hubCoverageData = await res.json();
    renderCoverageBadge(hubCoverageData);
    if (!document.getElementById('hub-coverage-drawer')?.classList.contains('hidden')) {
      renderCoverageDrawerContent(hubCoverageData);
    }
  } catch (err) {
    console.warn('Erro ao consultar /api/hub/coverage:', err);
  }
}

function renderCoverageBadge(data) {
  const badge = document.getElementById('hub-coverage-badge');
  const badgeText = document.getElementById('hub-coverage-badge-text');
  if (!badge || !badgeText) return;

  const pct = data.coverage_percentage ?? 0;
  badgeText.textContent = `${pct}% Cobertura`;

  if (data.ok) {
    badge.className = 'flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[10px] font-mono hover:bg-emerald-500/20 transition-all cursor-pointer shadow-sm';
    badge.title = `Fábrica alinhada (${pct}% das capacidades do owner no ar). Clique para ver pendências.`;
  } else {
    badge.className = 'flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-rose-500/10 text-rose-400 border border-rose-500/20 text-[10px] font-mono hover:bg-rose-500/20 transition-all cursor-pointer shadow-sm';
    badge.title = `Drift detectado! ${data.problems.length} pendências fora do gate. Clique para detalhes.`;
  }
}

function openCoverageDrawer() {
  const drawer = document.getElementById('hub-coverage-drawer');
  const panel = document.getElementById('hub-coverage-drawer-panel');
  if (!drawer || !panel) return;

  drawer.classList.remove('hidden');
  // Trigger slide-in transition
  requestAnimationFrame(() => {
    panel.classList.remove('translate-x-full');
    panel.classList.add('translate-x-0');
  });

  if (hubCoverageData) {
    renderCoverageDrawerContent(hubCoverageData);
  } else {
    loadHubCoverage();
  }
}

function closeCoverageDrawer() {
  const drawer = document.getElementById('hub-coverage-drawer');
  const panel = document.getElementById('hub-coverage-drawer-panel');
  if (!drawer || !panel) return;

  panel.classList.remove('translate-x-0');
  panel.classList.add('translate-x-full');
  setTimeout(() => {
    drawer.classList.add('hidden');
  }, 250);
}

function renderCoverageDrawerContent(data) {
  const container = document.getElementById('coverage-drawer-content');
  if (!container) return;

  const pct = data.coverage_percentage ?? 0;
  const surfaced = data.surfaced ?? 0;
  const pending = data.pending ?? 0;
  const waived = data.waived ?? 0;
  const total = data.total_owner_facing ?? (surfaced + pending);
  const ok = data.ok;
  const problems = data.problems || [];
  const pendingByRoadmap = data.pending_by_roadmap || {};
  const roadmapItems = data.roadmap_items || {};

  const horizonColors = {
    'Agora': 'bg-amber-500/10 text-amber-300 border-amber-500/20',
    'Depois': 'bg-purple-500/10 text-purple-300 border-purple-500/20',
    'Futuro': 'bg-slate-500/10 text-slate-300 border-slate-500/20'
  };

  let pendingSectionsHtml = '';
  const roadmapKeys = Object.keys(pendingByRoadmap);

  if (roadmapKeys.length === 0) {
    pendingSectionsHtml = `
      <div class="p-6 text-center text-slate-400 text-xs font-mono bg-slate-900/50 rounded-2xl border border-slate-800">
        Nenhuma capacidade pendente registrada no roadmap.
      </div>
    `;
  } else {
    pendingSectionsHtml = roadmapKeys.map(rId => {
      const meta = roadmapItems[rId] || { id: rId, title: 'Item do Roadmap', horizon: 'Depois' };
      const items = pendingByRoadmap[rId] || [];
      const horizonBadgeClass = horizonColors[meta.horizon] || horizonColors['Depois'];

      const itemsListHtml = items.map(it => `
        <li class="flex items-center gap-2 text-[11px] font-mono text-slate-300 py-1 border-b border-slate-800/40 last:border-b-0">
          <span class="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase ${it.kind === 'api' ? 'bg-indigo-500/20 text-indigo-300' : 'bg-cyan-500/20 text-cyan-300'}">${it.kind}</span>
          <span class="truncate" title="${it.key}">${it.key}</span>
        </li>
      `).join('');

      return `
        <div class="bg-slate-900/70 border border-slate-800/80 rounded-2xl p-4 shadow-sm">
          <div class="flex items-center justify-between gap-2 mb-2">
            <div class="flex items-center gap-2">
              <span class="font-mono font-bold text-xs text-white">${rId}</span>
              <span class="px-2 py-0.5 rounded-full border text-[10px] font-medium ${horizonBadgeClass}">${meta.horizon}</span>
            </div>
            <span class="text-[10px] font-mono text-slate-400">${items.length} ${items.length === 1 ? 'item' : 'itens'}</span>
          </div>
          <p class="text-xs font-medium text-slate-200 mb-3">${meta.title}</p>
          <ul class="bg-slate-950/60 rounded-xl p-2.5 border border-slate-800/50 space-y-0.5">
            ${itemsListHtml}
          </ul>
        </div>
      `;
    }).join('');
  }

  let problemsBannerHtml = '';
  if (!ok && problems.length > 0) {
    problemsBannerHtml = `
      <div class="p-3.5 mb-4 rounded-2xl bg-rose-500/10 border border-rose-500/20 text-rose-300 text-xs font-mono space-y-1">
        <div class="font-bold flex items-center gap-1.5 text-rose-400">
          <span>⚠️</span>
          <span>Gate com Problemas (${problems.length})</span>
        </div>
        <ul class="list-disc pl-4 space-y-0.5 text-[11px]">
          ${problems.map(p => `<li>${p}</li>`).join('')}
        </ul>
      </div>
    `;
  }

  container.innerHTML = `
    <!-- Drift / Gate Status Banner -->
    ${problemsBannerHtml}

    <!-- Progress Card -->
    <div class="p-4 rounded-2xl bg-gradient-to-br from-slate-900 to-slate-950 border border-slate-800/80 mb-5">
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs font-medium text-slate-400 font-mono">Cobertura Owner-Facing</span>
        <span class="text-base font-bold font-mono ${ok ? 'text-emerald-400' : 'text-amber-400'}">${pct}%</span>
      </div>
      <div class="w-full h-2 rounded-full bg-slate-800 overflow-hidden mb-4">
        <div class="h-full rounded-full transition-all duration-500 ${ok ? 'bg-gradient-to-r from-emerald-500 to-teal-400' : 'bg-gradient-to-r from-amber-500 to-rose-400'}" style="width: ${pct}%"></div>
      </div>

      <div class="grid grid-cols-3 gap-2 text-center pt-2 border-t border-slate-800/80">
        <div class="bg-slate-950/50 rounded-xl p-2 border border-slate-800/50">
          <div class="text-xs font-bold text-emerald-400 font-mono">${surfaced}</div>
          <div class="text-[10px] text-slate-400 font-mono">Entregues</div>
        </div>
        <div class="bg-slate-950/50 rounded-xl p-2 border border-slate-800/50">
          <div class="text-xs font-bold text-amber-400 font-mono">${pending}</div>
          <div class="text-[10px] text-slate-400 font-mono">Pendentes</div>
        </div>
        <div class="bg-slate-950/50 rounded-xl p-2 border border-slate-800/50">
          <div class="text-xs font-bold text-slate-400 font-mono">${waived}</div>
          <div class="text-[10px] text-slate-500 font-mono">Waivers</div>
        </div>
      </div>
    </div>

    <!-- Section Title & Refresh -->
    <div class="flex items-center justify-between mb-3 px-1">
      <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono">Pendências por Roadmap</h4>
      <button 
        onclick="loadHubCoverage(true)"
        title="Recarregar diagnóstico sem cache"
        class="text-[11px] font-mono text-indigo-400 hover:text-indigo-300 flex items-center gap-1 transition-colors">
        <span>🔄</span>
        <span>Recarregar</span>
      </button>
    </div>

    <!-- Roadmap Grouped Cards -->
    <div class="space-y-3">
      ${pendingSectionsHtml}
    </div>
  `;
}

// Global keyboard handler: close drawer on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeCoverageDrawer();
  }
});

// Auto-load on page load
document.addEventListener('DOMContentLoaded', () => {
  loadHubCoverage();
});
