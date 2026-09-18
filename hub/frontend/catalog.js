/**
 * Cross-Project Reusable Catalog & Factory Self-Evolution (HF-25) for DarkHub.
 * Read-only headless inspection and status visualization of reusable components
 * and self-evolution proposals.
 */

const catalogState = {
  components: [],
  evolution: null,
  loading: false,
};

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
          <h2 class="text-base font-semibold text-slate-100">Catálogo Cross-Projeto & Auto-Evolução (HF-25)</h2>
          <span class="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 border border-emerald-500/20">Onda 2</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">Componentes reutilizáveis compartilhados entre Atrium, Jarvis e DarkFac com salvaguardas de auto-evolução.</p>
      </div>
      <div class="flex items-center gap-2">
        <button id="refresh-catalog-btn" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>🔄 Atualizar</span>
        </button>
      </div>
    </div>
    <div id="catalog-components-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      <div class="col-span-full py-4 text-center text-xs text-slate-500">Carregando componentes reutilizáveis...</div>
    </div>
    <div id="evolution-status-container" class="mt-4 pt-3 border-t border-slate-800/60 text-xs text-slate-400">
    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  const btn = document.getElementById("refresh-catalog-btn");
  if (btn) {
    btn.addEventListener("click", () => loadCatalogData());
  }
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
    grid.innerHTML = `<div class="col-span-full py-4 text-center text-xs text-slate-500">Nenhum componente registrado.</div>`;
  } else {
    grid.innerHTML = catalogState.components.map((c) => `
      <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-3 flex flex-col justify-between hover:border-slate-700 transition">
        <div>
          <div class="flex items-center justify-between mb-1.5">
            <span class="text-xs font-semibold text-slate-200">${escapeHtml(c.name)}</span>
            <span class="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-mono text-slate-400">v${escapeHtml(c.version)}</span>
          </div>
          <p class="text-[11px] text-slate-400 leading-relaxed mb-2">${escapeHtml(c.description)}</p>
        </div>
        <div class="pt-2 border-t border-slate-800/50 flex items-center justify-between text-[10px] text-slate-500">
          <span>Origem: <strong class="text-slate-400">${escapeHtml(c.source_project_id)}</strong></span>
          <span class="rounded bg-emerald-950/40 px-1.5 py-0.5 text-emerald-400">${escapeHtml(c.kind)}</span>
        </div>
      </div>
    `).join("");
  }

  const evoContainer = document.getElementById("evolution-status-container");
  if (evoContainer && catalogState.evolution) {
    const evo = catalogState.evolution;
    evoContainer.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="font-medium text-slate-300">Subsistema de Auto-Evolução:</span>
        <div class="flex gap-3">
          <span>Propostas: <strong class="text-slate-200">${evo.total_proposals}</strong></span>
          <span>Ativas: <strong class="text-emerald-400">${evo.active_promotions}</strong></span>
          <span>Rejeitadas: <strong class="text-rose-400">${evo.rejected_count}</strong></span>
        </div>
      </div>
    `;
  }
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
