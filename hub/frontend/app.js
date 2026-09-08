/**
 * DarkHub - Frontend Application State & Controller
 */

// Category Definitions & Display Metadata
const CATEGORIES = {
  all: { label: "Todos", icon: "layout-grid" },
  favorites: { label: "Favoritos", icon: "star" },
  local_cluster: { label: "Clusters Locais", icon: "cpu" },
  llm_chat: { label: "LLMs & Chat", icon: "message-square" },
  code_agents: { label: "Código & Agentes", icon: "terminal" },
  infra_apis: { label: "Infra & APIs", icon: "server" },
  image_audio: { label: "Visão & Áudio", icon: "image" },
  custom: { label: "Personalizados", icon: "folder" },
};

// Application State
const state = {
  services: [],
  selectedCategory: "all",
  searchQuery: "",
  ollama: { is_online: false, models: [], model_count: 0 },
  openrouter: {
    has_key: false,
    is_authenticated: false,
    key_label: null,
    is_free_tier: false,
    usage_usd: 0,
    models: [],
  },
  playgroundProvider: "ollama",
  benchmarks: null,
  selectedBenchmarkProvider: "all",
  health: {},
  prompts: [],
  editingServiceId: null,
  paletteResults: [],
  paletteSelectedIndex: 0,
};

// API Base URL
const API_BASE = "/api";

// Initialize App on DOM Ready
document.addEventListener("DOMContentLoaded", () => {
  initApp();
});

async function initApp() {
  setupEventListeners();
  await Promise.all([
    loadServices(),
    loadOllamaStatus(),
    loadOpenRouterStatus(),
    loadPrompts(),
    loadBenchmarks(),
  ]);
  renderCategoryTabs();
  triggerBackgroundPings();
}

// Event Listeners Setup
function setupEventListeners() {
  // Global Keyboard Shortcuts
  window.addEventListener("keydown", (e) => {
    // Ctrl+K or Cmd+K or Slash / (when not inside input)
    if (
      (e.ctrlKey || e.metaKey) &&
      e.key.toLowerCase() === "k" &&
      !e.shiftKey
    ) {
      e.preventDefault();
      openCommandPalette();
    } else if (
      e.key === "/" &&
      document.activeElement.tagName !== "INPUT" &&
      document.activeElement.tagName !== "TEXTAREA"
    ) {
      e.preventDefault();
      openCommandPalette();
    } else if (e.key === "Escape") {
      closeAllModals();
    }
  });

  // Search input in header
  const searchInput = document.getElementById("search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      state.searchQuery = e.target.value.toLowerCase().trim();
      renderServices();
    });
  }

  // Command Palette Input
  const paletteInput = document.getElementById("palette-search-input");
  if (paletteInput) {
    paletteInput.addEventListener("input", handlePaletteSearch);
    paletteInput.addEventListener("keydown", handlePaletteKeyboardNav);
  }

  // Service Form Submission
  const serviceForm = document.getElementById("service-form");
  if (serviceForm) {
    serviceForm.addEventListener("submit", handleSaveService);
  }

  // Playground Run Button
  const playgroundRunBtn = document.getElementById("playground-run-btn");
  if (playgroundRunBtn) {
    playgroundRunBtn.addEventListener("click", handleRunPlayground);
  }
}

// Fetch Services from Backend
async function loadServices() {
  try {
    const res = await fetch(`${API_BASE}/services`);
    if (res.ok) {
      state.services = await res.json();
      renderQuickDock();
      renderServices();
    }
  } catch (err) {
    console.error("Failed to load services:", err);
    showToast("Erro ao carregar serviços", "error");
  }
}

// Fetch Ollama Status & Model Catalog
async function loadOllamaStatus() {
  try {
    const res = await fetch(`${API_BASE}/ollama/status`);
    if (res.ok) {
      state.ollama = await res.json();
      renderOllamaBadge();
      if (state.playgroundProvider === "ollama") {
        populatePlaygroundModels();
      }
    }
  } catch (err) {
    console.error("Failed to check Ollama:", err);
  }
}

// Fetch OpenRouter Status & Curated Models
async function loadOpenRouterStatus() {
  try {
    const res = await fetch(`${API_BASE}/openrouter/status`);
    if (res.ok) {
      state.openrouter = await res.json();
      renderOpenRouterBadge();
      if (state.playgroundProvider === "openrouter") {
        populatePlaygroundModels();
      }
    }
  } catch (err) {
    console.error("Failed to check OpenRouter:", err);
  }
}

// Render OpenRouter Status in Navbar
function renderOpenRouterBadge() {
  const badgeContainer = document.getElementById("openrouter-status-badge");
  if (!badgeContainer) return;

  if (state.openrouter.is_authenticated) {
    badgeContainer.innerHTML = `
      <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-400 text-xs font-mono" title="OpenRouter Conectado (${escapeHtml(state.openrouter.key_label || "Chave Ativa")})">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span class="hidden sm:inline">OpenRouter: Ativo ($${(state.openrouter.usage_usd || 0).toFixed(2)})</span>
      </div>
    `;
  } else if (state.openrouter.has_key) {
    badgeContainer.innerHTML = `
      <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-amber-950/60 border border-amber-500/30 text-amber-400 text-xs font-mono" title="${escapeHtml(state.openrouter.error || "Erro de chave")}">
        <span class="w-2 h-2 rounded-full bg-amber-400"></span>
        <span class="hidden sm:inline">OpenRouter: Falha Auth</span>
      </div>
    `;
  } else {
    badgeContainer.innerHTML = `
      <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-slate-800/80 border border-slate-700 text-slate-400 text-xs font-mono" title="Chave OPENROUTER_API_KEY não encontrada">
        <span class="w-2 h-2 rounded-full bg-slate-500"></span>
        <span class="hidden sm:inline">OpenRouter: Sem Chave</span>
      </div>
    `;
  }
}

// Fetch Daily Benchmarks Ledger
async function loadBenchmarks() {
  try {
    const res = await fetch(`${API_BASE}/benchmarks/latest`);
    if (res.ok) {
      state.benchmarks = await res.json();
      renderBenchmarksView();
    }
  } catch (err) {
    console.error("Failed to load benchmarks:", err);
  }
}


// Fetch Prompts Catalog
async function loadPrompts() {
  try {
    const res = await fetch(`${API_BASE}/prompts`);
    if (res.ok) {
      state.prompts = await res.json();
      renderPromptsList();
    }
  } catch (err) {
    console.error("Failed to load prompts:", err);
  }
}

// Background ping all services
async function triggerBackgroundPings() {
  try {
    const res = await fetch(`${API_BASE}/health/ping-all`);
    if (res.ok) {
      const results = await res.json();
      results.forEach((r) => {
        state.health[r.service_id] = r;
      });
      renderQuickDock();
      renderServices();
    }
  } catch (err) {
    console.warn("Background health check failed:", err);
  }
}

// Render Ollama Status in Navbar
function renderOllamaBadge() {
  const badgeContainer = document.getElementById("ollama-status-badge");
  if (!badgeContainer) return;

  if (state.ollama.is_online) {
    badgeContainer.innerHTML = `
      <div class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-400 text-xs font-mono">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span>Ollama: Online (${state.ollama.model_count} modelos)</span>
      </div>
    `;
  } else {
    badgeContainer.innerHTML = `
      <div class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-800/80 border border-slate-700 text-slate-400 text-xs font-mono">
        <span class="w-2 h-2 rounded-full bg-slate-500"></span>
        <span>Ollama: Offline</span>
      </div>
    `;
  }
}

// Render Category Tabs
function renderCategoryTabs() {
  const container = document.getElementById("category-tabs");
  if (!container) return;

  container.innerHTML = Object.entries(CATEGORIES)
    .map(([key, meta]) => {
      const isActive = state.selectedCategory === key;
      const count = getCategoryCount(key);
      return `
      <button 
        onclick="selectCategory('${key}')"
        class="flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
          isActive
            ? "bg-indigo-600 text-white shadow-lg shadow-indigo-500/25 border border-indigo-400/30"
            : "bg-slate-800/60 text-slate-400 hover:text-slate-200 hover:bg-slate-800 border border-slate-700/50"
        }">
        <span>${meta.label}</span>
        <span class="px-1.5 py-0.2 rounded-md ${
          isActive
            ? "bg-indigo-700/80 text-white"
            : "bg-slate-900/80 text-slate-500"
        } text-[10px] font-mono">${count}</span>
      </button>
    `;
    })
    .join("");
}

function getCategoryCount(catKey) {
  if (catKey === "all") return state.services.length;
  if (catKey === "favorites")
    return state.services.filter((s) => s.is_favorite).length;
  return state.services.filter((s) => s.category === catKey).length;
}

function selectCategory(catKey) {
  state.selectedCategory = catKey;
  renderCategoryTabs();
  renderServices();
}

// Render Quick Launch Dock (Pinned Services)
function renderQuickDock() {
  const container = document.getElementById("quick-dock-items");
  const section = document.getElementById("quick-dock-section");
  if (!container || !section) return;

  const pinned = state.services.filter(
    (service) => service.pinned && isSafeServiceId(service.id)
  );

  if (pinned.length === 0) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");
  container.innerHTML = pinned
    .map((item) => {
      const health = state.health[item.id];
      const statusDotClass = getStatusDotClass(health?.status);
      const latencyText = health?.latency_ms ? `${health.latency_ms}ms` : "";
      const color = safeServiceColor(item.color);
      const name = escapeHtml(item.name);
      const url = escapeHtml(safeServiceUrl(item.url) || "#");

      return `
      <a 
        href="${url}"
        target="_blank" 
        rel="noopener noreferrer"
        class="group relative flex items-center gap-3 px-3.5 py-2 rounded-xl glass-card hover:border-indigo-500/50 transition-all cursor-pointer">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center text-sm font-bold shadow-inner" style="background: ${
          color
        }22; color: ${color}; border: 1px solid ${color}44">
          ${escapeHtml(item.name.substring(0, 2).toUpperCase())}
        </div>
        <div class="flex flex-col min-w-0">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-semibold text-slate-200 group-hover:text-indigo-300 transition-colors truncate max-w-[120px]">${
              name
            }</span>
            <span class="w-1.5 h-1.5 rounded-full ${statusDotClass}"></span>
          </div>
          <span class="text-[10px] text-slate-400 font-mono">${
            escapeHtml(latencyText || (item.is_local ? "Local" : "Cloud"))
          }</span>
        </div>
      </a>
    `;
    })
    .join("");
}

// Render Main Services Grid
function renderServices() {
  const container = document.getElementById("services-grid");
  const emptyState = document.getElementById("empty-state");
  if (!container) return;

  let filtered = state.services.filter((item) => {
    if (!isSafeServiceId(item.id)) return false;
    // Category Filter
    if (state.selectedCategory === "favorites" && !item.is_favorite)
      return false;
    if (
      state.selectedCategory !== "all" &&
      state.selectedCategory !== "favorites" &&
      item.category !== state.selectedCategory
    )
      return false;

    // Search Query Filter
    if (state.searchQuery) {
      const q = state.searchQuery;
      const matchName = item.name.toLowerCase().includes(q);
      const matchDesc = item.description.toLowerCase().includes(q);
      const matchUrl = item.url.toLowerCase().includes(q);
      const matchTags = item.tags.some((t) => t.toLowerCase().includes(q));
      if (!matchName && !matchDesc && !matchUrl && !matchTags) return false;
    }

    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = "";
    if (emptyState) emptyState.classList.remove("hidden");
    return;
  }

  if (emptyState) emptyState.classList.add("hidden");

  container.innerHTML = filtered
    .map((item) => {
      const health = state.health[item.id];
      const statusDotClass = getStatusDotClass(health?.status);
      const latencyDisplay = health?.latency_ms
        ? `<span class="text-[10px] font-mono text-slate-400">${escapeHtml(String(health.latency_ms))}ms</span>`
        : "";
      const color = safeServiceColor(item.color);
      const name = escapeHtml(item.name);
      const serviceId = escapeHtml(item.id);
      const url = escapeHtml(safeServiceUrl(item.url) || "#");

      return `
      <div class="glass-card rounded-2xl p-5 flex flex-col justify-between group relative overflow-hidden">
        <!-- Accent Glow Header line -->
        <div class="absolute top-0 left-0 right-0 h-[2px]" style="background: linear-gradient(90deg, ${
          color
        }, transparent)"></div>

        <div>
          <!-- Top Row: Icon, Title, Actions -->
          <div class="flex items-start justify-between gap-3 mb-3">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl flex items-center justify-center font-bold text-base shadow-md" style="background: ${
                color
              }25; color: ${color}; border: 1px solid ${color}50">
                ${escapeHtml(item.name.substring(0, 2).toUpperCase())}
              </div>
              <div>
                <div class="flex items-center gap-2">
                  <h3 class="font-semibold text-slate-100 text-sm group-hover:text-indigo-300 transition-colors">${
                    name
                  }</h3>
                  <span class="w-2 h-2 rounded-full ${statusDotClass}" title="${
        escapeHtml(health?.status || "Status pendente")
      }"></span>
                </div>
                <div class="flex items-center gap-2 mt-0.5">
                  <span class="text-[10px] text-slate-400 font-mono">${
                    escapeHtml(CATEGORIES[item.category]?.label || item.category)
                  }</span>
                  ${latencyDisplay}
                </div>
              </div>
            </div>

            <!-- Card Actions -->
            <div class="flex items-center gap-1">
              <button 
                data-service-action="favorite"
                data-service-id="${serviceId}"
                title="${
                  item.is_favorite
                    ? "Remover dos favoritos"
                    : "Marcar como favorito"
                }"
                class="p-1.5 rounded-lg text-slate-400 hover:text-amber-400 hover:bg-slate-800/80 transition-colors">
                <svg class="w-4 h-4 ${
                  item.is_favorite
                    ? "text-amber-400 fill-amber-400"
                    : "fill-none"
                }" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>
                </svg>
              </button>

              <button 
                data-service-action="pin"
                data-service-id="${serviceId}"
                title="${
                  item.pinned ? "Desafixar do dock" : "Fixar no topo (dock)"
                }"
                class="p-1.5 rounded-lg text-slate-400 hover:text-indigo-400 hover:bg-slate-800/80 transition-colors">
                <svg class="w-4 h-4 ${
                  item.pinned ? "text-indigo-400 fill-indigo-400" : "fill-none"
                }" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                  <line x1="12" y1="17" x2="12" y2="22"></line>
                  <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path>
                </svg>
              </button>

              <div class="relative group/menu">
                <button class="p-1.5 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-slate-800/80 transition-colors">
                  <svg class="w-4 h-4" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="1"></circle>
                    <circle cx="12" cy="5" r="1"></circle>
                    <circle cx="12" cy="19" r="1"></circle>
                  </svg>
                </button>
                <div class="absolute right-0 top-full mt-1 w-28 bg-slate-900 border border-slate-700/80 rounded-xl shadow-xl py-1 hidden group-hover/menu:block z-20">
                  <button data-service-action="edit" data-service-id="${serviceId}" class="w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800 hover:text-white flex items-center gap-2">
                    Editar
                  </button>
                  <button data-service-action="delete" data-service-id="${serviceId}" class="w-full text-left px-3 py-1.5 text-xs text-red-400 hover:bg-slate-800 hover:text-red-300 flex items-center gap-2">
                    Excluir
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- Description -->
          <p class="text-xs text-slate-400 leading-relaxed mb-4 line-clamp-2">
            ${escapeHtml(item.description || "Nenhuma descrição fornecida.")}
          </p>
        </div>

        <!-- Card Footer: Tags + Launch Button -->
        <div>
          <!-- Tags -->
          <div class="flex flex-wrap gap-1.5 mb-4">
            ${item.tags
              .slice(0, 3)
              .map(
                (tag) => `
              <span class="px-2 py-0.5 rounded-md bg-slate-800/80 border border-slate-700/40 text-[10px] text-slate-400 font-mono">
                #${escapeHtml(tag)}
              </span>
            `
              )
              .join("")}
            ${
              item.tags.length > 3
                ? `<span class="text-[10px] text-slate-500 font-mono">+${
                    item.tags.length - 3
                  }</span>`
                : ""
            }
          </div>

          <!-- Launch Button -->
          <div class="flex items-center gap-2">
            <a 
              href="${url}"
              target="_blank" 
              rel="noopener noreferrer"
              class="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-xl bg-slate-800/90 hover:bg-indigo-600 hover:text-white text-slate-200 text-xs font-medium border border-slate-700/60 hover:border-indigo-500 transition-all shadow-sm">
              <span>Abrir Ferramenta</span>
              <svg class="w-3.5 h-3.5" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" fill="none">
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                <polyline points="15 3 21 3 21 9"></polyline>
                <line x1="10" y1="14" x2="21" y2="3"></line>
              </svg>
            </a>
            <button 
              data-service-action="copy-url"
              data-service-id="${serviceId}"
              title="Copiar URL"
              class="p-2 rounded-xl bg-slate-800/60 hover:bg-slate-700/80 text-slate-400 hover:text-slate-200 border border-slate-700/50 transition-colors">
              <svg class="w-3.5 h-3.5" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" fill="none">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            </button>
          </div>
        </div>
      </div>
    `;
    })
    .join("");

  bindServiceActions(container);
}

function getStatusDotClass(status) {
  if (status === "online") return "status-dot-online";
  if (status === "degraded") return "status-dot-degraded";
  if (status === "offline") return "status-dot-offline";
  return "status-dot-unknown";
}

const serviceActionContainers = new WeakSet();

function bindServiceActions(container) {
  if (serviceActionContainers.has(container)) return;
  container.addEventListener("click", (event) => {
    const control = event.target.closest("[data-service-action]");
    if (!control || !container.contains(control)) return;

    const serviceId = control.dataset.serviceId;
    if (!isSafeServiceId(serviceId)) return;

    const actions = {
      favorite: () => toggleFavorite(serviceId),
      pin: () => togglePin(serviceId),
      edit: () => editService(serviceId),
      delete: () => deleteService(serviceId),
      "copy-url": () => {
        const item = state.services.find((service) => service.id === serviceId);
        const url = item ? safeServiceUrl(item.url) : "";
        if (url) copyToClipboard(url, "Link copiado para a área de transferência!");
      },
    };
    actions[control.dataset.serviceAction]?.();
  });
  serviceActionContainers.add(container);
}

// Toggle Favorite
async function toggleFavorite(id) {
  try {
    const res = await fetch(`${API_BASE}/services/${id}/toggle-favorite`, {
      method: "POST",
    });
    if (res.ok) {
      const updated = await res.json();
      const idx = state.services.findIndex((s) => s.id === id);
      if (idx !== -1) state.services[idx] = updated;
      renderCategoryTabs();
      renderServices();
    }
  } catch (err) {
    showToast("Erro ao atualizar favorito", "error");
  }
}

// Toggle Pin
async function togglePin(id) {
  try {
    const res = await fetch(`${API_BASE}/services/${id}/toggle-pin`, {
      method: "POST",
    });
    if (res.ok) {
      const updated = await res.json();
      const idx = state.services.findIndex((s) => s.id === id);
      if (idx !== -1) state.services[idx] = updated;
      renderQuickDock();
      renderServices();
    }
  } catch (err) {
    showToast("Erro ao fixar serviço", "error");
  }
}

// Delete Service
async function deleteService(id) {
  if (!confirm("Tem certeza que deseja excluir este serviço?")) return;
  try {
    const res = await fetch(`${API_BASE}/services/${id}`, { method: "DELETE" });
    if (res.ok) {
      state.services = state.services.filter((s) => s.id !== id);
      delete state.health[id];
      renderCategoryTabs();
      renderQuickDock();
      renderServices();
      showToast("Serviço excluído com sucesso", "success");
    }
  } catch (err) {
    showToast("Erro ao excluir serviço", "error");
  }
}

// Open Service Modal for Add / Edit
function openServiceModal(serviceId = null) {
  state.editingServiceId = serviceId;
  const modal = document.getElementById("service-modal");
  const title = document.getElementById("modal-title");
  const form = document.getElementById("service-form");

  if (!modal || !form) return;

  if (serviceId) {
    const item = state.services.find((s) => s.id === serviceId);
    if (!item) return;
    title.textContent = "Editar Serviço";
    form.elements["name"].value = item.name;
    form.elements["url"].value = item.url;
    form.elements["category"].value = item.category;
    form.elements["description"].value = item.description || "";
    form.elements["tags"].value = item.tags.join(", ");
    form.elements["color"].value = item.color || "#3b82f6";
    form.elements["is_favorite"].checked = item.is_favorite;
    form.elements["pinned"].checked = item.pinned;
  } else {
    title.textContent = "Adicionar Novo Serviço";
    form.reset();
    form.elements["color"].value = "#3b82f6";
  }

  modal.classList.remove("hidden");
}

function closeServiceModal() {
  const modal = document.getElementById("service-modal");
  if (modal) modal.classList.add("hidden");
  state.editingServiceId = null;
}

function editService(id) {
  openServiceModal(id);
}

// Save Service (Create or Update)
async function handleSaveService(e) {
  e.preventDefault();
  const form = e.target;
  const rawTags = form.elements["tags"].value;
  const tags = rawTags
    ? rawTags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean)
    : [];

  const payload = {
    name: form.elements["name"].value.trim(),
    url: form.elements["url"].value.trim(),
    category: form.elements["category"].value,
    description: form.elements["description"].value.trim(),
    tags: tags,
    color: form.elements["color"].value,
    is_favorite: form.elements["is_favorite"].checked,
    pinned: form.elements["pinned"].checked,
  };

  try {
    if (state.editingServiceId) {
      // Update
      const res = await fetch(
        `${API_BASE}/services/${state.editingServiceId}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      if (res.ok) {
        const updated = await res.json();
        const idx = state.services.findIndex(
          (s) => s.id === state.editingServiceId
        );
        if (idx !== -1) state.services[idx] = updated;
        showToast("Serviço atualizado com sucesso!", "success");
      }
    } else {
      // Create
      const res = await fetch(`${API_BASE}/services`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const created = await res.json();
        state.services.push(created);
        showToast("Novo serviço adicionado ao Hub!", "success");
      }
    }

    closeServiceModal();
    renderCategoryTabs();
    renderQuickDock();
    renderServices();
  } catch (err) {
    console.error("Save failed:", err);
    showToast("Erro ao salvar serviço", "error");
  }
}

// ==========================================
// Command Palette (Ctrl+K / /)
// ==========================================
function openCommandPalette() {
  const modal = document.getElementById("command-palette-modal");
  const input = document.getElementById("palette-search-input");
  if (!modal || !input) return;

  modal.classList.remove("hidden");
  input.value = "";
  state.paletteResults = [...state.services];
  state.paletteSelectedIndex = 0;
  renderPaletteResults();
  setTimeout(() => input.focus(), 50);
}

function closeCommandPalette() {
  const modal = document.getElementById("command-palette-modal");
  if (modal) modal.classList.add("hidden");
}

function handlePaletteSearch(e) {
  const q = e.target.value.toLowerCase().trim();
  if (!q) {
    state.paletteResults = [...state.services];
  } else {
    state.paletteResults = state.services.filter((s) => {
      return (
        s.name.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q) ||
        s.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }
  state.paletteSelectedIndex = 0;
  renderPaletteResults();
}

function handlePaletteKeyboardNav(e) {
  if (state.paletteResults.length === 0) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    state.paletteSelectedIndex =
      (state.paletteSelectedIndex + 1) % state.paletteResults.length;
    renderPaletteResults();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    state.paletteSelectedIndex =
      (state.paletteSelectedIndex - 1 + state.paletteResults.length) %
      state.paletteResults.length;
    renderPaletteResults();
  } else if (e.key === "Enter") {
    e.preventDefault();
    const selected = state.paletteResults[state.paletteSelectedIndex];
    const url = selected ? safeServiceUrl(selected.url) : "";
    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
      closeCommandPalette();
    }
  }
}

function renderPaletteResults() {
  const list = document.getElementById("palette-results-list");
  if (!list) return;

  if (state.paletteResults.length === 0) {
    list.innerHTML = `
      <div class="p-8 text-center text-slate-500 text-xs font-mono">
        Nenhuma ferramenta encontrada para a busca.
      </div>
    `;
    return;
  }

  list.innerHTML = state.paletteResults
    .map((item, idx) => {
      const isSelected = idx === state.paletteSelectedIndex;
      const color = safeServiceColor(item.color);
      return `
      <div 
        onclick="launchPaletteItem(${idx})"
        class="flex items-center justify-between px-4 py-3 rounded-xl cursor-pointer transition-all ${
          isSelected
            ? "bg-indigo-600/30 border border-indigo-500/50 text-white"
            : "text-slate-300 hover:bg-slate-800/60"
        }">
        <div class="flex items-center gap-3 min-w-0">
          <div class="w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold shrink-0" style="background: ${
            color
          }25; color: ${color}">
            ${escapeHtml(item.name.substring(0, 2).toUpperCase())}
          </div>
          <div class="min-w-0">
            <div class="text-xs font-medium text-slate-200 truncate">${
              escapeHtml(item.name)
            }</div>
            <div class="text-[10px] text-slate-400 truncate">${
              escapeHtml(item.description)
            }</div>
          </div>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span class="text-[10px] font-mono text-slate-500">${
            escapeHtml(CATEGORIES[item.category]?.label || item.category)
          }</span>
          <span class="px-1.5 py-0.5 rounded bg-slate-800 text-[10px] font-mono text-slate-400">↵ Abrir</span>
        </div>
      </div>
    `;
    })
    .join("");

  // Scroll active into view
  const activeEl = list.children[state.paletteSelectedIndex];
  if (activeEl) {
    activeEl.scrollIntoView({ block: "nearest" });
  }
}

function launchPaletteItem(idx) {
  const item = state.paletteResults[idx];
  const url = item ? safeServiceUrl(item.url) : "";
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
    closeCommandPalette();
  }
}

/// ==========================================
// Unified AI Playground (Ollama & OpenRouter)
// ==========================================
function openPlaygroundDrawer() {
  const drawer = document.getElementById("playground-drawer");
  if (drawer) drawer.classList.remove("hidden");
  populatePlaygroundModels();
}

function closePlaygroundDrawer() {
  const drawer = document.getElementById("playground-drawer");
  if (drawer) drawer.classList.add("hidden");
}

function setPlaygroundProvider(provider) {
  state.playgroundProvider = provider;

  const tabOllama = document.getElementById("provider-tab-ollama");
  const tabOpenRouter = document.getElementById("provider-tab-openrouter");
  const footerNote = document.getElementById("playground-footer-note");
  const statusBadge = document.getElementById("playground-provider-status");

  if (provider === "ollama") {
    if (tabOllama) {
      tabOllama.className =
        "py-1.5 px-3 rounded-lg text-xs font-mono font-medium transition-all bg-indigo-600 text-white shadow-sm flex items-center justify-center gap-1.5";
    }
    if (tabOpenRouter) {
      tabOpenRouter.className =
        "py-1.5 px-3 rounded-lg text-xs font-mono font-medium transition-all text-slate-400 hover:text-slate-200 flex items-center justify-center gap-1.5";
    }
    if (footerNote) {
      footerNote.textContent =
        "Rodando no hardware local via Ollama (Custo: $0)";
    }
    if (statusBadge) {
      statusBadge.textContent = state.ollama.is_online ? "Online" : "Offline";
      statusBadge.className = `text-[10px] font-mono ${
        state.ollama.is_online ? "text-emerald-400" : "text-slate-500"
      }`;
    }
  } else {
    // OpenRouter
    if (tabOpenRouter) {
      tabOpenRouter.className =
        "py-1.5 px-3 rounded-lg text-xs font-mono font-medium transition-all bg-indigo-600 text-white shadow-sm flex items-center justify-center gap-1.5";
    }
    if (tabOllama) {
      tabOllama.className =
        "py-1.5 px-3 rounded-lg text-xs font-mono font-medium transition-all text-slate-400 hover:text-slate-200 flex items-center justify-center gap-1.5";
    }
    if (footerNote) {
      footerNote.textContent =
        "Inferência na Nuvem via OpenRouter Gateway (Preços em tempo real)";
    }
    if (statusBadge) {
      statusBadge.textContent = state.openrouter.is_authenticated
        ? "Conectado"
        : "Chave Ausente";
      statusBadge.className = `text-[10px] font-mono ${
        state.openrouter.is_authenticated
          ? "text-emerald-400"
          : "text-amber-400"
      }`;
    }
  }

  populatePlaygroundModels();
}

function togglePlaygroundAdvanced() {
  const panel = document.getElementById("playground-advanced-panel");
  const arrow = document.getElementById("advanced-toggle-arrow");
  if (!panel || !arrow) return;

  const isHidden = panel.classList.contains("hidden");
  if (isHidden) {
    panel.classList.remove("hidden");
    arrow.textContent = "▼";
  } else {
    panel.classList.add("hidden");
    arrow.textContent = "▶";
  }
}

function populatePlaygroundModels() {
  const select = document.getElementById("playground-model-select");
  if (!select) return;

  if (state.playgroundProvider === "ollama") {
    if (state.ollama.models && state.ollama.models.length > 0) {
      select.innerHTML = state.ollama.models
        .map(
          (m) => `
        <option value="${m.name}">[Local $0] ${m.name} (${
            m.parameter_size || "quantizado"
          })</option>
      `
        )
        .join("");
    } else {
      select.innerHTML = `<option value="">Nenhum modelo local detectado (Ollama offline)</option>`;
    }
  } else {
    // OpenRouter provider
    if (state.openrouter.models && state.openrouter.models.length > 0) {
      select.innerHTML = state.openrouter.models
        .map(
          (m) => `
        <option value="${m.id}">[Cloud] ${m.name} ${
            m.is_pareto ? "🏆 Pareto" : ""
          }</option>
      `
        )
        .join("");
    } else {
      select.innerHTML = `<option value="">Nenhum modelo OpenRouter configurado</option>`;
    }
  }
}

async function handleRunPlayground() {
  const modelSelect = document.getElementById("playground-model-select");
  const promptInput = document.getElementById("playground-prompt-input");
  const systemInput = document.getElementById("playground-system-input");
  const tempInput = document.getElementById("playground-temperature");
  const maxTokensInput = document.getElementById("playground-max-tokens");
  const outputContainer = document.getElementById("playground-output");
  const runBtn = document.getElementById("playground-run-btn");

  if (!modelSelect || !promptInput || !outputContainer || !runBtn) return;

  const model = modelSelect.value;
  const prompt = promptInput.value.trim();
  const system = systemInput ? systemInput.value.trim() : null;
  const temperature = tempInput ? parseFloat(tempInput.value) : 0.7;
  const max_tokens = maxTokensInput ? parseInt(maxTokensInput.value, 10) : 1024;
  const provider = state.playgroundProvider;

  if (!model) {
    showToast("Selecione um modelo válido para executar", "error");
    return;
  }
  if (!prompt) {
    showToast("Digite um prompt para executar", "error");
    return;
  }

  runBtn.disabled = true;
  runBtn.innerHTML = `
    <span class="inline-block animate-spin mr-2">⟳</span>
    <span>Inferindo no ${provider === "ollama" ? "Ollama" : "OpenRouter"}...</span>
  `;
  outputContainer.classList.remove("hidden");
  outputContainer.innerHTML = `
    <div class="text-xs text-slate-400 font-mono animate-pulse">
      Aguardando resposta do modelo ${model} via ${provider}...
    </div>
  `;

  try {
    const res = await fetch(`${API_BASE}/playground/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        model,
        prompt,
        system: system || null,
        temperature,
        max_tokens,
      }),
    });

    if (res.ok) {
      const data = await res.json();
      const costDisplay =
        data.cost_usd !== null && data.cost_usd !== undefined
          ? `$${data.cost_usd.toFixed(4)}`
          : provider === "ollama"
          ? "$0.00 (Local)"
          : "";
      const tokensDisplay = data.tokens_used
        ? `${data.tokens_used} tokens`
        : "";

      outputContainer.innerHTML = `
        <div class="flex items-center justify-between border-b border-slate-800 pb-2.5 mb-3 text-[11px] font-mono">
          <div class="flex items-center gap-2">
            <span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Sucesso</span>
            <span class="text-slate-400">${data.total_duration_ms || 0}ms</span>
            ${
              tokensDisplay
                ? `<span class="text-slate-500">• ${tokensDisplay}</span>`
                : ""
            }
            ${
              costDisplay
                ? `<span class="text-amber-400">• ${costDisplay}</span>`
                : ""
            }
          </div>
          <button onclick="copyToClipboard(document.getElementById('playground-text-res').innerText, 'Resposta copiada!')" class="text-indigo-400 hover:text-indigo-300 transition-colors">
            Copiar Resposta
          </button>
        </div>
        <div id="playground-text-res" class="text-xs text-slate-200 whitespace-pre-wrap leading-relaxed font-mono select-text">${escapeHtml(
          data.response
        )}</div>
      `;
    } else {
      const err = await res.json();
      outputContainer.innerHTML = `
        <div class="text-xs text-red-400 font-mono">
          [Falha na inferência]: ${err.detail || "Erro desconhecido na API"}
        </div>
      `;
    }
  } catch (err) {
    outputContainer.innerHTML = `
      <div class="text-xs text-red-400 font-mono">Erro ao conectar ao endpoint do playground: ${err}</div>
    `;
  } finally {
    runBtn.disabled = false;
    runBtn.innerHTML = `<span>Executar Prompt</span>`;
  }
}

// ==========================================
// Prompt Snippet Vault
// ==========================================
function openPromptVaultDrawer() {
  const drawer = document.getElementById("prompt-vault-drawer");
  if (drawer) drawer.classList.remove("hidden");
}

function closePromptVaultDrawer() {
  const drawer = document.getElementById("prompt-vault-drawer");
  if (drawer) drawer.classList.add("hidden");
}

function renderPromptsList() {
  const container = document.getElementById("prompt-vault-list");
  if (!container) return;

  container.innerHTML = state.prompts
    .map(
      (p) => `
    <div class="glass-panel p-4 rounded-xl border border-slate-800 flex flex-col justify-between group">
      <div>
        <div class="flex items-start justify-between gap-2 mb-1.5">
          <h4 class="text-xs font-semibold text-slate-200">${escapeHtml(p.title)}</h4>
          <button 
            onclick="copyPromptContent('${p.id}')"
            class="px-2.5 py-1 rounded-lg bg-indigo-600/30 hover:bg-indigo-600 text-indigo-300 hover:text-white text-[10px] font-mono border border-indigo-500/30 transition-all">
            Copiar
          </button>
        </div>
        <p class="text-[11px] text-slate-400 mb-3">${escapeHtml(p.description)}</p>
        <div class="p-2.5 rounded-lg bg-slate-900/90 text-slate-300 font-mono text-[10px] whitespace-pre-wrap border border-slate-800/80 max-h-24 overflow-y-auto mb-2">
${escapeHtml(p.content)}
        </div>
      </div>
      <div class="flex items-center justify-between mt-2 pt-2 border-t border-slate-800/50">
        <div class="flex gap-1">
          ${p.tags
            .map(
              (t) =>
                `<span class="text-[9px] text-slate-500 font-mono">#${escapeHtml(t)}</span>`
            )
            .join(" ")}
        </div>
        <button onclick="sendPromptToPlayground('${
          p.id
        }')" class="text-[10px] text-slate-400 hover:text-slate-200 font-mono">
          Usar no Playground →
        </button>
      </div>
    </div>
  `
    )
    .join("");
}

function copyPromptContent(promptId) {
  const prompt = state.prompts.find((p) => p.id === promptId);
  if (prompt) {
    copyToClipboard(prompt.content, `Prompt '${prompt.title}' copiado!`);
  }
}

function sendPromptToPlayground(promptId) {
  const prompt = state.prompts.find((p) => p.id === promptId);
  if (!prompt) return;

  closePromptVaultDrawer();
  openPlaygroundDrawer();
  const promptInput = document.getElementById("playground-prompt-input");
  if (promptInput) {
    promptInput.value = prompt.content;
    promptInput.focus();
  }
}

// ==========================================
// Benchmarks & Fronteira de Pareto Modal
// ==========================================
function openBenchmarksModal() {
  const modal = document.getElementById("benchmarks-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  if (!state.benchmarks) {
    loadBenchmarks();
  } else {
    renderBenchmarksView();
  }
}

function closeBenchmarksModal() {
  const modal = document.getElementById("benchmarks-modal");
  if (modal) modal.classList.add("hidden");
}

async function refreshBenchmarksData() {
  const btn = document.getElementById("benchmarks-refresh-btn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="inline-block animate-spin mr-1.5">⟳</span><span>Atualizando...</span>`;
  }

  try {
    const res = await fetch(`${API_BASE}/benchmarks/refresh`, { method: "POST" });
    if (res.ok) {
      state.benchmarks = await res.json();
      renderBenchmarksView();
      showToast("Benchmarks atualizados com sucesso!", "success");
    } else {
      showToast("Falha ao atualizar benchmarks", "error");
    }
  } catch (err) {
    showToast(`Erro na requisição: ${err}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span class="refresh-icon">🔄</span><span>Atualizar Hoje</span>`;
    }
  }
}

function renderBenchmarksView() {
  if (!state.benchmarks) return;

  // Date badge
  const dateBadge = document.getElementById("benchmarks-date-badge");
  if (dateBadge && state.benchmarks.date) {
    dateBadge.textContent = state.benchmarks.date;
  }

  const rawModels = state.benchmarks.models || {};
  const modelsList = Object.values(rawModels);

  // Top summary stats
  const totalEl = document.getElementById("bench-stat-total");
  const paretoCodingEl = document.getElementById("bench-stat-pareto-coding");
  const paretoGeneralEl = document.getElementById("bench-stat-pareto-general");

  if (totalEl) totalEl.textContent = modelsList.length;

  const paretoCodingCount = modelsList.filter((m) => m.is_pareto_coding).length;
  const paretoGeneralCount = modelsList.filter((m) => m.is_pareto_general).length;

  if (paretoCodingEl) paretoCodingEl.textContent = paretoCodingCount;
  if (paretoGeneralEl) paretoGeneralEl.textContent = paretoGeneralCount;

  // Extract unique providers for filter tabs
  const providersSet = new Set(modelsList.map((m) => m.provider).filter(Boolean));
  const providers = ["all", ...Array.from(providersSet).sort()];

  const filtersContainer = document.getElementById("benchmarks-provider-filters");
  if (filtersContainer) {
    filtersContainer.innerHTML = providers
      .map((p) => {
        const isActive = state.selectedBenchmarkProvider === p;
        const label = p === "all" ? "Todos" : p.charAt(0).toUpperCase() + p.slice(1);
        return `
        <button 
          onclick="selectBenchmarkProvider('${p}')"
          class="px-3 py-1 rounded-lg text-xs font-mono transition-all ${
            isActive
              ? "bg-amber-500/20 text-amber-300 border border-amber-500/40 font-semibold"
              : "bg-slate-800/60 text-slate-400 hover:text-slate-200 border border-slate-700/40"
          }">
          ${label}
        </button>
      `;
      })
      .join("");
  }

  // Filter models
  let filtered = modelsList;
  if (state.selectedBenchmarkProvider !== "all") {
    filtered = filtered.filter((m) => m.provider === state.selectedBenchmarkProvider);
  }

  // Sort by coding_score descending
  filtered.sort((a, b) => (b.coding_score || 0) - (a.coding_score || 0));

  const tableBody = document.getElementById("benchmarks-table-body");
  if (!tableBody) return;

  if (filtered.length === 0) {
    tableBody.innerHTML = `
      <tr>
        <td colspan="8" class="py-8 text-center text-slate-500 text-xs">
          Nenhum modelo encontrado para o fabricante selecionado.
        </td>
      </tr>
    `;
    return;
  }

  tableBody.innerHTML = filtered
    .map((m) => {
      const codingBar = m.coding_score
        ? `<div class="flex items-center gap-2">
             <div class="w-16 bg-slate-800 h-1.5 rounded-full overflow-hidden">
               <div class="bg-indigo-500 h-full rounded-full" style="width: ${m.coding_score}%"></div>
             </div>
             <span class="text-xs font-semibold text-slate-200">${m.coding_score.toFixed(1)}</span>
           </div>`
        : `<span class="text-slate-600">--</span>`;

      const intelBar = m.intelligence_score
        ? `<div class="flex items-center gap-2">
             <div class="w-16 bg-slate-800 h-1.5 rounded-full overflow-hidden">
               <div class="bg-amber-500 h-full rounded-full" style="width: ${m.intelligence_score}%"></div>
             </div>
             <span class="text-xs font-semibold text-slate-200">${m.intelligence_score.toFixed(1)}</span>
           </div>`
        : `<span class="text-slate-600">--</span>`;

      const costText =
        m.cost_per_task !== null && m.cost_per_task !== undefined
          ? `$${m.cost_per_task.toFixed(4)}`
          : "$0.00";

      const speedText = m.output_speed_tps
        ? `${m.output_speed_tps.toFixed(0)} t/s`
        : "--";
      const ttftText = m.latency_ttft_sec
        ? `${m.latency_ttft_sec.toFixed(2)}s`
        : "";

      let paretoBadges = [];
      if (m.is_pareto_coding) {
        paretoBadges.push(
          `<span class="px-2 py-0.5 rounded-md bg-indigo-500/15 text-indigo-400 border border-indigo-500/30 text-[10px]">🏆 Código</span>`
        );
      }
      if (m.is_pareto_general) {
        paretoBadges.push(
          `<span class="px-2 py-0.5 rounded-md bg-amber-500/15 text-amber-400 border border-amber-500/30 text-[10px]">🧠 Geral</span>`
        );
      }
      if (paretoBadges.length === 0) {
        paretoBadges.push(`<span class="text-slate-600 text-[10px]">-</span>`);
      }

      return `
      <tr class="hover:bg-slate-800/40 transition-colors">
        <td class="py-3 pr-2">
          <div class="font-semibold text-slate-200 text-xs">${escapeHtml(m.name || m.model_id)}</div>
          <div class="text-[10px] text-slate-500 truncate max-w-[200px]">${escapeHtml(m.model_id)}</div>
        </td>
        <td class="py-3 px-2 text-slate-400 capitalize">${escapeHtml(m.provider || "N/A")}</td>
        <td class="py-3 px-2">${codingBar}</td>
        <td class="py-3 px-2">${intelBar}</td>
        <td class="py-3 px-2 text-slate-300 font-mono">${costText}</td>
        <td class="py-3 px-2 text-slate-400">
          <div>${speedText}</div>
          <div class="text-[10px] text-slate-500">${ttftText}</div>
        </td>
        <td class="py-3 px-2">
          <div class="flex flex-wrap gap-1">${paretoBadges.join(" ")}</div>
        </td>
        <td class="py-3 pl-2 text-right">
          <button 
            onclick="sendModelToPlayground('${m.model_id}')"
            class="px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600 text-indigo-300 hover:text-white border border-indigo-500/30 text-[10px] transition-all whitespace-nowrap">
            Testar →
          </button>
        </td>
      </tr>
    `;
    })
    .join("");
}

function selectBenchmarkProvider(provider) {
  state.selectedBenchmarkProvider = provider;
  renderBenchmarksView();
}

function sendModelToPlayground(modelId) {
  closeBenchmarksModal();
  setPlaygroundProvider("openrouter");
  openPlaygroundDrawer();

  // If model is already in select options, select it, otherwise add it as option
  const select = document.getElementById("playground-model-select");
  if (select) {
    let exists = false;
    for (let opt of select.options) {
      if (opt.value === modelId) {
        exists = true;
        break;
      }
    }
    if (!exists) {
      const opt = document.createElement("option");
      opt.value = modelId;
      opt.textContent = `[Cloud] ${modelId} (Fronteira)`;
      select.appendChild(opt);
    }
    select.value = modelId;
  }
}

// ==========================================
// Backup & Configurações Modal
// ==========================================
function openBackupModal() {
  const modal = document.getElementById("backup-modal");
  if (modal) modal.classList.remove("hidden");
}

function closeBackupModal() {
  const modal = document.getElementById("backup-modal");
  if (modal) modal.classList.add("hidden");
}

async function handleExportCatalog() {
  try {
    const res = await fetch(`${API_BASE}/services/export`);
    if (res.ok) {
      const data = await res.json();
      const dateStr = new Date().toISOString().split("T")[0];
      const filename = `darkhub-services-backup-${dateStr}.json`;
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`Backup baixado: ${filename}`, "success");
    } else {
      showToast("Falha ao exportar catálogo", "error");
    }
  } catch (err) {
    showToast(`Erro na exportação: ${err}`, "error");
  }
}

async function handleImportFileSelected(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const mergeCheckbox = document.getElementById("import-merge-checkbox");
  const merge = mergeCheckbox ? mergeCheckbox.checked : false;

  try {
    const text = await file.text();
    const parsed = JSON.parse(text);

    let servicesToImport = [];
    if (Array.isArray(parsed)) {
      servicesToImport = parsed;
    } else if (parsed && Array.isArray(parsed.services)) {
      servicesToImport = parsed.services;
    } else {
      showToast("Formato inválido de JSON. Esperado lista de serviços.", "error");
      return;
    }

    const res = await fetch(`${API_BASE}/services/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ services: servicesToImport, merge }),
    });

    if (res.ok) {
      const result = await res.json();
      showToast(result.message || "Importação concluída!", "success");
      closeBackupModal();
      await loadServices();
    } else {
      const err = await res.json();
      showToast(`Erro na importação: ${err.detail || "Falha"}`, "error");
    }
  } catch (err) {
    showToast(`Erro ao processar arquivo: ${err}`, "error");
  } finally {
    event.target.value = "";
  }
}

async function handleResetDefaults() {
  if (
    !confirm(
      "Atenção: Tem certeza que deseja restaurar o catálogo padrão da DarkFac? Um backup automático será criado."
    )
  ) {
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/services/reset-defaults`, {
      method: "POST",
    });
    if (res.ok) {
      const result = await res.json();
      showToast(result.message, "success");
      closeBackupModal();
      await loadServices();
    } else {
      const err = await res.json();
      showToast(`Erro ao restaurar: ${err.detail || "Falha"}`, "error");
    }
  } catch (err) {
    showToast(`Erro na requisição: ${err}`, "error");
  }
}

// Helpers
function closeAllModals() {
  closeCommandPalette();
  closeServiceModal();
  closePlaygroundDrawer();
  closePromptVaultDrawer();
  closeBenchmarksModal();
  closeBackupModal();
}

function copyToClipboard(text, successMsg = "Copiado!") {
  navigator.clipboard
    .writeText(text)
    .then(() => {
      showToast(successMsg, "success");
    })
    .catch((err) => {
      showToast("Falha ao copiar", "error");
    });
}

function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  const bgClass =
    type === "success"
      ? "bg-emerald-950/90 border-emerald-500/50 text-emerald-300"
      : type === "error"
      ? "bg-red-950/90 border-red-500/50 text-red-300"
      : "bg-slate-900/90 border-indigo-500/50 text-indigo-300";

  toast.className = `flex items-center gap-2 px-4 py-2.5 rounded-xl border shadow-xl text-xs font-medium animate-in-quick ${bgClass}`;
  toast.innerHTML = `<span>${escapeHtml(message)}</span>`;

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(-8px)";
    toast.style.transition = "all 0.25s ease";
    setTimeout(() => toast.remove(), 250);
  }, 2800);
}

function escapeHtml(text) {
  if (text === null || text === undefined) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function isSafeServiceId(value) {
  return /^[a-z0-9](?:[a-z0-9_-]{0,98}[a-z0-9])?$/.test(String(value || ""));
}

function safeServiceColor(value) {
  const color = String(value || "");
  return /^#[0-9a-fA-F]{6}$/.test(color) ? color : "#3b82f6";
}

function safeServiceUrl(value) {
  const candidate = String(value || "");
  if (/\s/.test(candidate)) return "";
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? candidate
      : "";
  } catch (_error) {
    return "";
  }
}
