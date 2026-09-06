/**
 * DarkHub - Operational roadmap projection.
 *
 * This file only renders the snapshot returned by the read-only API. It does
 * not create, reorder, or infer roadmap items in the browser.
 */

const roadmapUi = {
  projectId: "darkfac",
  projects: [],
  snapshot: null,
  mode: "overview",
  selectedItemId: null,
  loading: false,
};

const ROADMAP_LABELS = {
  lifecycle_stage: {
    foundations: "Fundações",
    execution: "Execução atual",
    next_steps: "Próximas etapas",
    future: "Futuro",
  },
  item_type: {
    epic: "Épico",
    feature: "Feature",
    infrastructure: "Infraestrutura",
    research: "Pesquisa",
    quality: "Qualidade",
    security: "Segurança",
    documentation: "Documentação",
    operations: "Operação",
  },
  delivery_status: {
    discovered: "Descoberto",
    accepted: "Aceito",
    planned: "Planejado",
    implementing: "Em implementação",
    validating: "Em validação",
    completed: "Concluído",
    cancelled: "Cancelado",
  },
  horizon: {
    now: "Agora",
    next: "Próximo",
    later: "Mais adiante",
    exploratory: "Exploratório",
    unscheduled: "Sem previsão",
  },
  confidence: {
    high: "Alta",
    medium: "Média",
    low: "Baixa",
    unknown: "Desconhecida",
  },
};

document.addEventListener("DOMContentLoaded", () => {
  loadRoadmapProjects();
  const drawer = document.getElementById("roadmap-drawer");
  if (drawer) {
    drawer.addEventListener("click", (event) => {
      if (event.target === drawer) closeRoadmapDrawer();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("roadmap-drawer")?.classList.contains("hidden")) {
      closeRoadmapDrawer();
    }
  });
});

async function loadRoadmapProjects() {
  try {
    const response = await fetch("/api/projects");
    if (!response.ok) throw new Error("Não foi possível consultar os projetos");
    roadmapUi.projects = await response.json();
    const selected = roadmapUi.projects.find((project) => project.id === roadmapUi.projectId);
    roadmapUi.projectId = selected ? selected.id : roadmapUi.projects[0]?.id || "";
    renderRoadmapProjects();
  } catch (error) {
    console.warn("Roadmap project list unavailable:", error);
    renderRoadmapProjectsError(error.message);
  }
}

function openRoadmapDrawer() {
  const drawer = document.getElementById("roadmap-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  document.body.classList.add("overflow-hidden");
  renderRoadmapProjects();
  loadRoadmapSnapshot();
  setTimeout(() => document.getElementById("roadmap-project-select")?.focus(), 0);
}

function closeRoadmapDrawer() {
  const drawer = document.getElementById("roadmap-drawer");
  if (drawer) drawer.classList.add("hidden");
  document.body.classList.remove("overflow-hidden");
  roadmapUi.selectedItemId = null;
}

function renderRoadmapProjects() {
  const select = document.getElementById("roadmap-project-select");
  if (!select) return;
  if (!roadmapUi.projects.length) {
    select.innerHTML = '<option value="">Nenhum projeto disponível</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = roadmapUi.projects
    .map((project) => `<option value="${escapeRoadmapHtml(project.id)}">${escapeRoadmapHtml(project.name)}</option>`)
    .join("");
  select.value = roadmapUi.projectId;
  select.onchange = () => {
    roadmapUi.projectId = select.value;
    roadmapUi.selectedItemId = null;
    loadRoadmapSnapshot();
  };
}

function renderRoadmapProjectsError(message) {
  const select = document.getElementById("roadmap-project-select");
  if (select) {
    select.innerHTML = '<option value="">Indisponível</option>';
    select.disabled = true;
  }
  renderRoadmapAlert(`Projeto indisponível: ${message}`, "error");
}

async function loadRoadmapSnapshot() {
  if (!roadmapUi.projectId) return;
  roadmapUi.loading = true;
  renderRoadmapLoading();
  const params = new URLSearchParams();
  const filters = [
    ["search", "roadmap-search-input"],
    ["item_type", "roadmap-type-filter"],
    ["lifecycle_stage", "roadmap-stage-filter"],
    ["delivery_status", "roadmap-status-filter"],
    ["horizon", "roadmap-horizon-filter"],
    ["confidence", "roadmap-confidence-filter"],
  ];
  filters.forEach(([name, id]) => {
    const value = document.getElementById(id)?.value || "";
    if (value) params.set(name, value);
  });
  try {
    const query = params.toString() ? `?${params.toString()}` : "";
    const response = await fetch(`/api/projects/${encodeURIComponent(roadmapUi.projectId)}/roadmap${query}`);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || "Não foi possível consultar o snapshot");
    }
    roadmapUi.snapshot = await response.json();
    roadmapUi.loading = false;
    renderRoadmapSnapshot();
  } catch (error) {
    roadmapUi.loading = false;
    roadmapUi.snapshot = null;
    renderRoadmapAlert(`Snapshot indisponível: ${error.message}`, "error");
    renderRoadmapContent("Não há uma projeção disponível para este projeto.");
  }
}

function renderRoadmapLoading() {
  setRoadmapText("roadmap-total-items", "…");
  setRoadmapText("roadmap-confirmed-items", "…");
  setRoadmapText("roadmap-blocked-items", "…");
  setRoadmapText("roadmap-conflict-items", "…");
  const overview = document.getElementById("roadmap-overview");
  const table = document.getElementById("roadmap-table-view");
  if (overview) overview.innerHTML = '<div class="roadmap-empty-state">Consultando fontes canônicas…</div>';
  if (table) table.innerHTML = "";
}

function renderRoadmapSnapshot() {
  const snapshot = roadmapUi.snapshot;
  if (!snapshot) return;
  const stats = snapshot.stats || {};
  setRoadmapText("roadmap-total-items", String(stats.total_items ?? snapshot.items.length));
  setRoadmapText("roadmap-confirmed-items", String(stats.confirmed_items ?? 0));
  setRoadmapText("roadmap-blocked-items", String(stats.blocked_items ?? 0));
  setRoadmapText("roadmap-conflict-items", String(stats.conflict_items ?? 0));
  setRoadmapText("roadmap-observed-at", formatRoadmapDate(snapshot.observed_at));
  setRoadmapText("roadmap-snapshot-hash", (snapshot.snapshot_hash || "").slice(0, 12));

  const unavailable = snapshot.sources_unavailable || [];
  if (unavailable.length) {
    renderRoadmapAlert(`Fonte indisponível: ${unavailable.join(", ")}. O último snapshot está marcado como possivelmente obsoleto.`, "warning");
  } else if ((snapshot.issues || []).length) {
    renderRoadmapAlert(`${snapshot.issues.length} aviso(s) de consistência acompanham este snapshot.`, "warning");
  } else {
    renderRoadmapAlert("Snapshot compilado das fontes canônicas; nenhuma inconsistência detectada.", "success");
  }
  renderRoadmapContent();
  renderRoadmapSources(snapshot.sources_consulted || []);
}

function renderRoadmapContent(emptyMessage = "Nenhum item corresponde aos filtros.") {
  const snapshot = roadmapUi.snapshot;
  const overview = document.getElementById("roadmap-overview");
  const timeline = document.getElementById("roadmap-timeline-view");
  const dependencies = document.getElementById("roadmap-dependencies-view");
  const table = document.getElementById("roadmap-table-view");
  if (!snapshot || !snapshot.items?.length) {
    if (overview) overview.innerHTML = `<div class="roadmap-empty-state">${escapeRoadmapHtml(emptyMessage)}</div>`;
    if (timeline) timeline.innerHTML = `<div class="roadmap-empty-state">${escapeRoadmapHtml(emptyMessage)}</div>`;
    if (dependencies) dependencies.innerHTML = `<div class="roadmap-empty-state">${escapeRoadmapHtml(emptyMessage)}</div>`;
    if (table) table.innerHTML = `<div class="roadmap-empty-state">${escapeRoadmapHtml(emptyMessage)}</div>`;
    return;
  }
  if (overview) overview.innerHTML = renderRoadmapOverview(snapshot.items);
  if (timeline) timeline.innerHTML = renderRoadmapTimeline(snapshot.items);
  if (dependencies) dependencies.innerHTML = renderRoadmapDependencies(snapshot.items);
  if (table) table.innerHTML = renderRoadmapTable(snapshot.items);
  selectRoadmapMode(roadmapUi.mode, false);
}

function renderRoadmapOverview(items) {
  const groups = ["foundations", "execution", "next_steps", "future"];
  return groups
    .map((stage) => {
      const stageItems = items.filter((item) => item.lifecycle_stage === stage);
      if (!stageItems.length) return "";
      return `
        <section class="space-y-3" aria-labelledby="roadmap-stage-${stage}">
          <div class="flex items-center justify-between gap-3">
            <h3 id="roadmap-stage-${stage}" class="text-xs font-mono font-semibold uppercase tracking-[0.18em] text-slate-400">${roadmapLabel("lifecycle_stage", stage)}</h3>
            <span class="text-[10px] text-slate-500 font-mono">${stageItems.length} item(ns)</span>
          </div>
          <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
            ${stageItems.map(renderRoadmapCard).join("")}
          </div>
        </section>
      `;
    })
    .join("") || '<div class="roadmap-empty-state">Nenhuma macroetapa disponível.</div>';
}

function renderRoadmapTimeline(items) {
  const horizons = ["now", "next", "later", "exploratory", "unscheduled"];
  const columns = horizons.map((horizon) => {
    const horizonItems = items.filter((item) => item.horizon === horizon);
    return `
      <section class="roadmap-timeline-column" aria-labelledby="roadmap-horizon-${horizon}">
        <div class="flex items-center justify-between gap-2 mb-3">
          <h3 id="roadmap-horizon-${horizon}" class="text-xs font-semibold text-slate-300">${roadmapLabel("horizon", horizon)}</h3>
          <span class="text-[10px] text-slate-500 font-mono">${horizonItems.length}</span>
        </div>
        <div class="space-y-2">
          ${horizonItems.length ? horizonItems.map((item) => {
            const dates = item.planned_start || item.target_date
              ? `${formatRoadmapDate(item.planned_start)} → ${formatRoadmapDate(item.target_date)}`
              : "Sem data registrada";
            return `<article class="roadmap-timeline-item"><button type="button" class="w-full text-left" onclick="selectRoadmapItem('${escapeRoadmapAttribute(item.id)}')"><span class="roadmap-id">${escapeRoadmapHtml(item.id)}</span><span class="block mt-1 text-xs font-semibold text-slate-200">${escapeRoadmapHtml(item.title)}</span><span class="block mt-2 text-[10px] text-slate-500 font-mono">${dates}</span><span class="roadmap-tag roadmap-tag-status mt-2">${roadmapStatusSymbol(item.delivery_status)} ${roadmapLabel("delivery_status", item.delivery_status)}</span></button></article>`;
          }).join("") : '<p class="text-[10px] text-slate-600 font-mono">Nenhum item</p>'}
        </div>
      </section>
    `;
  }).join("");
  return `<div class="roadmap-timeline-intro">Horizonte sem data não é convertido em calendário. Datas abaixo só aparecem quando a fonte as registra.</div><div class="roadmap-timeline">${columns}</div>`;
}

function renderRoadmapDependencies(items) {
  const stages = ["foundations", "execution", "next_steps", "future"];
  const known = new Set(items.map((item) => item.id));
  const positions = {};
  let maxRows = 1;
  stages.forEach((stage, column) => {
    const stageItems = items.filter((item) => item.lifecycle_stage === stage);
    maxRows = Math.max(maxRows, stageItems.length);
    stageItems.forEach((item, row) => {
      positions[item.id] = { x: column * 315 + 20, y: row * 104 + 42 };
    });
  });
  const width = Math.max(1280, stages.length * 315 + 20);
  const height = Math.max(150, maxRows * 104 + 62);
  const edges = [];
  items.forEach((item) => (item.dependencies || []).forEach((dependency) => {
    if (dependency.type === "related_to" || !known.has(dependency.item_id)) return;
    const fromId = dependency.type === "requires" ? dependency.item_id : item.id;
    const toId = dependency.type === "requires" ? item.id : dependency.item_id;
    const from = positions[fromId];
    const to = positions[toId];
    if (!from || !to) return;
    edges.push(`<path class="roadmap-graph-edge" d="M ${from.x + 230} ${from.y + 30} C ${from.x + 275} ${from.y + 30}, ${to.x - 45} ${to.y + 30}, ${to.x} ${to.y + 30}" marker-end="url(#roadmap-arrow)" aria-label="${escapeRoadmapHtml(fromId)} ${escapeRoadmapHtml(roadmapDependencyLabel(dependency.type))} ${escapeRoadmapHtml(toId)}"></path>`);
  }));
  const nodes = items.map((item) => {
    const position = positions[item.id];
    const hasConflict = (item.operational_flags || []).includes("conflicting");
    const stroke = hasConflict ? "#fb7185" : "#22d3ee";
    return `<g class="roadmap-graph-node" tabindex="0" role="button" aria-label="Abrir ${escapeRoadmapHtml(item.title)}" onclick="selectRoadmapItem('${escapeRoadmapAttribute(item.id)}')" onkeydown="if(event.key==='Enter'||event.key===' ') selectRoadmapItem('${escapeRoadmapAttribute(item.id)}')"><rect x="${position.x}" y="${position.y}" width="230" height="60" rx="12" fill="#0f172a" stroke="${stroke}" stroke-opacity=".65"></rect><text x="${position.x + 12}" y="${position.y + 21}" fill="#67e8f9" font-size="10" font-family="JetBrains Mono, monospace">${escapeRoadmapHtml(item.id)}</text><text x="${position.x + 12}" y="${position.y + 41}" fill="#e2e8f0" font-size="11" font-family="Inter, sans-serif">${escapeRoadmapHtml(item.title).slice(0, 30)}${item.title.length > 30 ? "…" : ""}</text></g>`;
  }).join("");
  return `<div class="roadmap-graph-intro">Relações <code>related_to</code> ficam fora do cálculo causal. Clique ou pressione Enter em um nó para abrir o drawer de evidências.</div><div class="roadmap-graph-shell"><svg class="roadmap-graph" viewBox="0 0 ${width} ${height}" role="img" aria-label="Grafo direcionado de dependências do roadmap"><defs><marker id="roadmap-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#67e8f9"></path></marker></defs>${edges.join("")}${nodes}</svg></div>`;
}

function renderRoadmapCard(item) {
  const flags = item.operational_flags || [];
  const dependencies = item.dependencies || [];
  const sources = item.source_refs || [];
  const flagMarkup = flags.length
    ? flags.map((flag) => `<span class="roadmap-tag roadmap-tag-warning">⚠ ${escapeRoadmapHtml(roadmapFlagLabel(flag))}</span>`).join("")
    : '<span class="roadmap-tag roadmap-tag-neutral">● Sem alerta</span>';
  return `
    <article class="roadmap-card" data-roadmap-item-id="${escapeRoadmapHtml(item.id)}">
      <button type="button" class="w-full text-left" onclick="selectRoadmapItem('${escapeRoadmapAttribute(item.id)}')" aria-label="Abrir detalhes de ${escapeRoadmapHtml(item.title)}">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2 mb-2">
              <span class="roadmap-id">${escapeRoadmapHtml(item.id)}</span>
              ${flagMarkup}
            </div>
            <h4 class="text-sm font-semibold text-slate-100 leading-snug">${escapeRoadmapHtml(item.title)}</h4>
            <p class="mt-1.5 text-xs text-slate-400 leading-relaxed line-clamp-2">${escapeRoadmapHtml(item.description || "Sem descrição registrada.")}</p>
          </div>
          <span class="roadmap-state-symbol" aria-hidden="true">${roadmapStatusSymbol(item.delivery_status)}</span>
        </div>
        <div class="flex flex-wrap gap-1.5 mt-4">
          <span class="roadmap-tag roadmap-tag-status">${roadmapLabel("delivery_status", item.delivery_status)}</span>
          <span class="roadmap-tag roadmap-tag-neutral">${roadmapLabel("horizon", item.horizon)}</span>
          <span class="roadmap-tag roadmap-tag-neutral">${roadmapLabel("item_type", item.item_type)}</span>
        </div>
        <div class="mt-3 pt-3 border-t border-slate-800/80 flex items-center justify-between gap-3 text-[10px] text-slate-500 font-mono">
          <span>${dependencies.length ? `${dependencies.length} dependência(s)` : "Sem dependências diretas"}</span>
          <span>${sources.length ? `${sources.length} fonte(s)` : "Sem fonte confirmável"}</span>
        </div>
      </button>
    </article>
  `;
}

function renderRoadmapTable(items) {
  return `
    <div class="overflow-x-auto rounded-2xl border border-slate-800">
      <table class="w-full text-left text-xs" aria-label="Tabela acessível do roadmap operacional">
        <thead class="bg-slate-950/80 text-[10px] uppercase tracking-wider text-slate-500 font-mono">
          <tr>
            <th scope="col" class="px-4 py-3">Item</th>
            <th scope="col" class="px-4 py-3">Tipo</th>
            <th scope="col" class="px-4 py-3">Estado</th>
            <th scope="col" class="px-4 py-3">Horizonte</th>
            <th scope="col" class="px-4 py-3">Dependências</th>
            <th scope="col" class="px-4 py-3">Proveniência</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/70">
          ${items.map((item) => `
            <tr class="hover:bg-slate-800/40 focus-within:bg-slate-800/40">
              <th scope="row" class="px-4 py-3 min-w-[240px]">
                <button type="button" class="text-left group" onclick="selectRoadmapItem('${escapeRoadmapAttribute(item.id)}')">
                  <span class="roadmap-id">${escapeRoadmapHtml(item.id)}</span>
                  <span class="block mt-1 text-slate-200 font-semibold group-hover:text-indigo-300">${escapeRoadmapHtml(item.title)}</span>
                </button>
              </th>
              <td class="px-4 py-3 text-slate-400">${roadmapLabel("item_type", item.item_type)}</td>
              <td class="px-4 py-3"><span class="roadmap-tag roadmap-tag-status">${roadmapStatusSymbol(item.delivery_status)} ${roadmapLabel("delivery_status", item.delivery_status)}</span></td>
              <td class="px-4 py-3 text-slate-400">${roadmapLabel("horizon", item.horizon)}</td>
              <td class="px-4 py-3 text-slate-400 font-mono">${(item.dependencies || []).length}</td>
              <td class="px-4 py-3 text-slate-400 font-mono">${(item.source_refs || []).length}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

async function selectRoadmapItem(itemId) {
  roadmapUi.selectedItemId = itemId;
  const detail = document.getElementById("roadmap-detail");
  if (!detail) return;
  detail.classList.remove("hidden");
  detail.innerHTML = '<div class="roadmap-empty-state">Carregando proveniência…</div>';
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(roadmapUi.projectId)}/roadmap/items/${encodeURIComponent(itemId)}`);
    if (!response.ok) throw new Error("Item não encontrado no snapshot");
    const item = await response.json();
    detail.innerHTML = renderRoadmapDetail(item);
    detail.querySelector("button")?.focus();
  } catch (error) {
    detail.innerHTML = `<div class="roadmap-empty-state">${escapeRoadmapHtml(error.message)}</div>`;
  }
}

function closeRoadmapDetail() {
  roadmapUi.selectedItemId = null;
  document.getElementById("roadmap-detail")?.classList.add("hidden");
}

function renderRoadmapDetail(item) {
  const dependencyMarkup = (item.dependencies || []).length
    ? item.dependencies.map((dependency) => `<li><span class="roadmap-id">${escapeRoadmapHtml(dependency.item_id)}</span> <span class="text-slate-400">${roadmapDependencyLabel(dependency.type)}</span></li>`).join("")
    : '<li class="text-slate-500">Nenhuma dependência direta registrada.</li>';
  const criteria = (item.completion_criteria || []).length
    ? item.completion_criteria.map((criterion) => `<li>${escapeRoadmapHtml(criterion)}</li>`).join("")
    : '<li class="text-slate-500">Nenhum critério registrado.</li>';
  const evidence = (item.evidence_refs || []).length
    ? item.evidence_refs.map((ref) => `<li><span class="text-slate-200">${escapeRoadmapHtml(ref.label)}</span><span class="block text-[10px] text-slate-500 font-mono">${escapeRoadmapHtml(ref.locator)}</span></li>`).join("")
    : '<li class="text-slate-500">Nenhuma evidência de execução vinculada.</li>';
  const sources = (item.source_refs || []).length
    ? item.source_refs.map((ref) => `<li><a class="text-indigo-300 hover:text-indigo-200 underline underline-offset-2" target="_blank" rel="noopener noreferrer" href="/api/projects/${encodeURIComponent(roadmapUi.projectId)}/roadmap/sources/${encodeURIComponent(ref.source_id)}">${escapeRoadmapHtml(ref.label)}</a><span class="block text-[10px] text-slate-500 font-mono">${escapeRoadmapHtml(ref.locator)}${ref.produced_by ? ` · produzido por ${escapeRoadmapHtml(ref.produced_by)}` : ""}</span></li>`).join("")
    : '<li class="text-amber-300">Item sem fonte navegável; não confirmado.</li>';
  const downstream = (item.downstream_item_ids || []).length ? item.downstream_item_ids.join(", ") : "Nenhum";
  return `
    <div class="p-5 space-y-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <span class="roadmap-id">${escapeRoadmapHtml(item.id)}</span>
          <h3 class="mt-2 text-base font-bold text-white">${escapeRoadmapHtml(item.title)}</h3>
        </div>
        <button type="button" onclick="closeRoadmapDetail()" aria-label="Fechar detalhe" class="p-1.5 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-slate-800">✕</button>
      </div>
      <div class="flex flex-wrap gap-1.5">
        <span class="roadmap-tag roadmap-tag-status">${roadmapStatusSymbol(item.delivery_status)} ${roadmapLabel("delivery_status", item.delivery_status)}</span>
        <span class="roadmap-tag roadmap-tag-neutral">${roadmapLabel("horizon", item.horizon)}</span>
        <span class="roadmap-tag roadmap-tag-neutral">Confiança: ${roadmapLabel("confidence", item.confidence)}</span>
        <span class="roadmap-tag roadmap-tag-neutral">${escapeRoadmapHtml(item.confirmation_state || "unconfirmed")}</span>
      </div>
      <p class="text-xs text-slate-300 leading-relaxed">${escapeRoadmapHtml(item.description || "Sem descrição registrada.")}</p>
      <section><h4 class="roadmap-detail-heading">Justificativa do estado</h4><p class="text-xs text-slate-400 leading-relaxed">${escapeRoadmapHtml(item.state_rationale || "A fonte canônica não registra uma justificativa adicional para este estado.")}</p></section>
      <div class="grid grid-cols-2 gap-3 text-[11px]">
        <div class="rounded-xl bg-slate-950/60 border border-slate-800 p-3"><span class="block text-slate-500">Tipo</span><span class="text-slate-200">${roadmapLabel("item_type", item.item_type)}</span></div>
        <div class="rounded-xl bg-slate-950/60 border border-slate-800 p-3"><span class="block text-slate-500">Downstream</span><span class="text-slate-200 font-mono">${escapeRoadmapHtml(downstream)}</span></div>
      </div>
      <section><h4 class="roadmap-detail-heading">Dependências recebidas</h4><ul class="roadmap-detail-list">${dependencyMarkup}</ul></section>
      <section><h4 class="roadmap-detail-heading">Critérios de conclusão</h4><ul class="roadmap-detail-list">${criteria}</ul></section>
      <section><h4 class="roadmap-detail-heading">Evidências</h4><ul class="roadmap-detail-list">${evidence}</ul></section>
      <section><h4 class="roadmap-detail-heading">Fontes navegáveis</h4><ul class="roadmap-detail-list">${sources}</ul></section>
      <div class="pt-3 border-t border-slate-800 text-[10px] text-slate-500 font-mono">Revisão: ${escapeRoadmapHtml(item.source_revision || "não informada")} · Verificado: ${formatRoadmapDate(item.last_verified_at)}</div>
    </div>
  `;
}

function renderRoadmapSources(sources) {
  const container = document.getElementById("roadmap-sources");
  if (!container) return;
  container.innerHTML = sources.length
    ? sources.map((source) => `<span class="roadmap-source-pill">${source.status === "available" ? "●" : "⚠"} ${escapeRoadmapHtml(source.label)}</span>`).join("")
    : '<span class="text-slate-500">Nenhuma fonte consultada.</span>';
}

function selectRoadmapMode(mode, reload = true) {
  roadmapUi.mode = mode;
  const overview = document.getElementById("roadmap-overview");
  const timeline = document.getElementById("roadmap-timeline-view");
  const dependencies = document.getElementById("roadmap-dependencies-view");
  const table = document.getElementById("roadmap-table-view");
  const overviewButton = document.getElementById("roadmap-mode-overview");
  const timelineButton = document.getElementById("roadmap-mode-timeline");
  const dependenciesButton = document.getElementById("roadmap-mode-dependencies");
  const tableButton = document.getElementById("roadmap-mode-table");
  const isOverview = mode === "overview";
  const isTimeline = mode === "timeline";
  const isDependencies = mode === "dependencies";
  const isTable = mode === "table";
  overview?.classList.toggle("hidden", !isOverview);
  timeline?.classList.toggle("hidden", !isTimeline);
  dependencies?.classList.toggle("hidden", !isDependencies);
  table?.classList.toggle("hidden", !isTable);
  overviewButton?.classList.toggle("roadmap-mode-active", isOverview);
  timelineButton?.classList.toggle("roadmap-mode-active", isTimeline);
  dependenciesButton?.classList.toggle("roadmap-mode-active", isDependencies);
  tableButton?.classList.toggle("roadmap-mode-active", isTable);
  overviewButton?.setAttribute("aria-selected", String(isOverview));
  timelineButton?.setAttribute("aria-selected", String(isTimeline));
  dependenciesButton?.setAttribute("aria-selected", String(isDependencies));
  tableButton?.setAttribute("aria-selected", String(isTable));
  if (reload && roadmapUi.snapshot) renderRoadmapContent();
}

function clearRoadmapFilters() {
  ["roadmap-search-input", "roadmap-type-filter", "roadmap-stage-filter", "roadmap-status-filter", "roadmap-horizon-filter", "roadmap-confidence-filter"].forEach((id) => {
    const element = document.getElementById(id);
    if (element) element.value = "";
  });
  loadRoadmapSnapshot();
}

function renderRoadmapAlert(message, type) {
  const alert = document.getElementById("roadmap-alert");
  if (!alert) return;
  alert.className = `roadmap-alert roadmap-alert-${type}`;
  alert.textContent = message;
}

function setRoadmapText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function roadmapLabel(group, value) {
  return escapeRoadmapHtml(ROADMAP_LABELS[group]?.[value] || value || "Não informado");
}

function roadmapFlagLabel(value) {
  return ({ blocked: "Bloqueado", at_risk: "Em risco", conflicting: "Conflitante", obsolete: "Obsoleto" })[value] || value;
}

function roadmapDependencyLabel(value) {
  return ({ requires: "requer", blocks: "bloqueia", unlocks: "desbloqueia", parent_of: "é pai de", related_to: "relacionado a" })[value] || value;
}

function roadmapStatusSymbol(value) {
  return ({ discovered: "○", accepted: "◌", planned: "◷", implementing: "◉", validating: "◒", completed: "✓", cancelled: "×" })[value] || "•";
}

function formatRoadmapDate(value) {
  if (!value) return "não informado";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "não informado";
  return date.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function escapeRoadmapHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeRoadmapAttribute(value) {
  return String(value ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}
