/**
 * DarkHub - secoes recolhiveis (USR-219).
 *
 * Estado lembrado por navegador em localStorage (uma chave por secao). Todo acesso ao
 * armazenamento vai em try/catch: sem storage a pagina continua funcionando, recolhida.
 */

const HUB_COLLAPSE_PREFIX = "darkhub.collapse.";

function hubCollapseIsCollapsed(key, defaultCollapsed = true) {
  try {
    const raw = window.localStorage.getItem(HUB_COLLAPSE_PREFIX + key);
    if (raw === "open") return false;
    if (raw === "closed") return true;
  } catch (_err) {
    // storage indisponivel: usa o padrao
  }
  return defaultCollapsed;
}

function hubCollapseRemember(key, collapsed) {
  try {
    window.localStorage.setItem(HUB_COLLAPSE_PREFIX + key, collapsed ? "closed" : "open");
  } catch (_err) {
    // storage indisponivel: o estado vale so ate recarregar
  }
}

function hubCollapseLabel(collapsed) {
  return collapsed ? "Abrir" : "Fechar";
}

// Botao abrir/fechar; o corpo a esconder e o elemento de id `targetId`.
function hubCollapseButtonHtml(key, targetId, collapsed) {
  return `<button type="button" data-collapse-key="${key}" data-collapse-target="${targetId}" aria-expanded="${collapsed ? "false" : "true"}" aria-controls="${targetId}" onclick="hubToggleCollapse(this)" class="inline-flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-[11px] font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">${hubCollapseLabel(collapsed)}</button>`;
}

function hubToggleCollapse(button) {
  const key = button.getAttribute("data-collapse-key");
  const body = document.getElementById(button.getAttribute("data-collapse-target"));
  if (!key || !body) return;
  const collapsed = !body.classList.contains("hidden");
  body.classList.toggle("hidden", collapsed);
  button.setAttribute("aria-expanded", collapsed ? "false" : "true");
  button.textContent = hubCollapseLabel(collapsed);
  hubCollapseRemember(key, collapsed);
  if (!collapsed && typeof button.hubOnOpen === "function") button.hubOnOpen();
}

window.hubCollapseIsCollapsed = hubCollapseIsCollapsed;
window.hubCollapseButtonHtml = hubCollapseButtonHtml;
window.hubToggleCollapse = hubToggleCollapse;
