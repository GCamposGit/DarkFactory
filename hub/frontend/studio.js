/**
 * Anti-AI-Slop Content Engine & Context-Aware Visual Asset Studio (Skill 15 / Skill 16 / DH-07).
 * Provides high-signal content generation, deterministic linting for clichés and cadence monotony,
 * visual asset generation, gallery inspection, and semantically coupled text-to-visual illustration.
 */

const studioState = {
  presets: [],
  gallery: [],
  selectedPreset: "linkedin_post",
  selectedPersona: "staff_engineer",
  currentContent: "",
  lastLint: null,
  generatingContent: false,
  lintingContent: false,
  generatingVisual: false,
  illustrating: false,
  lastVisual: null,
};

function getStudioToken() {
  return window.sessionToken || window.state?.sessionToken || localStorage.getItem("darkhub_session_token") || null;
}

async function studioFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getStudioToken();
  if (token) {
    headers.set("X-Hub-Session", token);
  }
  return fetch(url, { ...options, headers });
}

function initStudio() {
  mountStudioSection();
  loadStudioData();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initStudio);
} else {
  initStudio();
}

function mountStudioSection() {
  if (document.getElementById("content-studio-section")) return;

  const targetAnchor =
    document.getElementById("cross-catalog-section") ||
    document.getElementById("infrastructure-cards-section") ||
    document.getElementById("api-credits-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "content-studio-section";
  section.className =
    "space-y-6 rounded-2xl border border-purple-900/40 bg-slate-900/40 p-4 sm:p-6 shadow-lg shadow-purple-950/10";
  section.innerHTML = `
    <!-- Header -->
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-purple-500/10 text-purple-400 text-sm">🎨</span>
          <h2 class="text-base font-semibold text-slate-100">Estúdio de Conteúdo Anti-Slop & Ateliê Visual (DH-07)</h2>
          <span class="rounded-full bg-purple-500/10 px-2.5 py-0.5 text-xs font-medium text-purple-300 border border-purple-500/20 font-mono">Skill 15 & 16</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">
          Geração de texto calibrada por persona com auditoria de pureza léxica e estúdio de ativos visuais contextuais a custo zero ($0).
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button id="btn-refresh-studio" type="button" class="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-700 hover:text-white transition">
          <span>🔄 Atualizar Estúdio</span>
        </button>
      </div>
    </div>

    <!-- Dual Column Grid -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

      <!-- Column 1: Anti-Slop Content Engine (Skill 15 / core.content / core.marketing) -->
      <div class="space-y-4 rounded-xl border border-slate-800/80 bg-slate-950/40 p-4">
        <div class="flex items-center justify-between border-b border-slate-800/60 pb-2">
          <h3 class="text-xs font-mono font-semibold uppercase tracking-wider text-purple-300 flex items-center gap-2">
            <span>✍️ Motor Anti-AI-Slop</span>
          </h3>
          <span id="content-status-pill" class="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-400">Pronto</span>
        </div>

        <!-- Controls: Preset & Persona -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Formato / Preset</label>
            <select id="studio-preset-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-purple-500">
              <option value="linkedin_post">LinkedIn Post (High Signal)</option>
              <option value="technical_blog">Technical Blog (Staff+)</option>
              <option value="b2b_proposal">B2B Commercial Proposal</option>
              <option value="release_notes">Release Notes & Changelog</option>
              <option value="executive_memo">Executive Architecture Memo</option>
            </select>
          </div>
          <div>
            <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Calibração de Persona</label>
            <select id="studio-persona-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-purple-500">
              <option value="staff_engineer">Staff+ Principal Engineer</option>
              <option value="executive_b2b">Executive Enterprise Leader</option>
              <option value="founder">Technical Founder & Operator</option>
              <option value="indie_hacker">Indie Hacker / Builder</option>
            </select>
          </div>
        </div>

        <!-- Topic / Prompt -->
        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Tópico ou Briefing Central</label>
          <div class="flex gap-2">
            <input 
              id="studio-topic-input" 
              type="text" 
              placeholder="Ex: Arquitetura de microagentes sem frameworks com garantia de idempotência no Windows" 
              class="flex-1 bg-slate-900 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono focus:border-purple-500"
            />
            <button 
              id="btn-generate-content" 
              type="button" 
              class="px-3.5 py-1.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-medium transition shadow-md shadow-purple-600/30 whitespace-nowrap active:scale-95 disabled:opacity-50">
              Gerar Texto
            </button>
          </div>
        </div>

        <!-- Content Editor -->
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="text-[10px] font-mono uppercase text-slate-400">Texto Gerado / Editor</label>
            <div class="flex items-center gap-2">
              <span id="content-word-count" class="text-[10px] font-mono text-slate-500">0 palavras</span>
              <button 
                id="btn-lint-content" 
                type="button" 
                class="px-2.5 py-0.5 rounded-lg border border-purple-500/40 bg-purple-500/10 text-purple-300 hover:bg-purple-500/20 text-[11px] font-mono transition">
                🔍 Auditar Slop (Lint)
              </button>
            </div>
          </div>
          <textarea 
            id="studio-content-textarea" 
            rows="6" 
            placeholder="O conteúdo gerado aparecerá aqui. Você também pode colar seu texto para auditar e expurgar clichês e ritmo monótono de IA..." 
            class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-xs text-slate-200 font-mono focus:border-purple-500 leading-relaxed"></textarea>
        </div>

        <!-- Lint Metrics & Pureza Result -->
        <div id="studio-lint-panel" class="hidden rounded-xl border border-slate-800 bg-slate-900/60 p-3 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-[11px] font-medium text-slate-300">Auditoria Anti-Slop (Pureza Léxica):</span>
            <span id="lint-pure-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold">—</span>
          </div>
          <div class="grid grid-cols-2 gap-2 text-[10px] font-mono text-slate-400">
            <div>Variância Rítmica: <strong id="lint-variance" class="text-slate-200">—</strong></div>
            <div>Clichês Detectados: <strong id="lint-cliches-count" class="text-slate-200">0</strong></div>
          </div>
          <div id="lint-cliches-list" class="text-[10px] font-mono text-rose-400 flex flex-wrap gap-1"></div>
          <div id="lint-recommendations" class="text-[10px] text-slate-400 italic"></div>
        </div>
      </div>

      <!-- Column 2: Context-Aware Visual Studio & Gallery (Skill 16 / core.visual) -->
      <div class="space-y-4 rounded-xl border border-slate-800/80 bg-slate-950/40 p-4">
        <div class="flex items-center justify-between border-b border-slate-800/60 pb-2">
          <h3 class="text-xs font-mono font-semibold uppercase tracking-wider text-indigo-300 flex items-center gap-2">
            <span>🖼️ Ateliê Visual & Galeria</span>
          </h3>
          <button 
            id="btn-illustrate-coupled" 
            type="button" 
            class="px-2.5 py-1 rounded-lg border border-indigo-500/40 bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/20 text-[11px] font-mono transition flex items-center gap-1">
            <span>✨ Ilustrar Texto do Editor</span>
          </button>
        </div>

        <!-- Visual Generation Controls -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <div>
            <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Tipo de Ativo</label>
            <select id="visual-type-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-indigo-500">
              <option value="social_banner">Social Banner</option>
              <option value="architecture_diagram">Diagrama de Arquitetura</option>
              <option value="blog_hero">Hero para Blog</option>
              <option value="ui_mockup">UI Mockup Blueprint</option>
            </select>
          </div>
          <div>
            <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Aspect Ratio</label>
            <select id="visual-ratio-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-indigo-500">
              <option value="16:9">16:9 (Widescreen)</option>
              <option value="1:1">1:1 (Quadrado)</option>
              <option value="4:3">4:3 (Apresentação)</option>
            </select>
          </div>
          <div>
            <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Ação</label>
            <button 
              id="btn-generate-visual" 
              type="button" 
              class="w-full py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition shadow-md shadow-indigo-600/30 whitespace-nowrap active:scale-95 disabled:opacity-50">
              Renderizar Ativo
            </button>
          </div>
        </div>

        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Prompt Visual Específico</label>
          <input 
            id="visual-prompt-input" 
            type="text" 
            placeholder="Ex: Dark glassmorphism cloud architecture with pipeline flow" 
            class="w-full bg-slate-900 border border-slate-800 rounded-xl px-3 py-1.5 text-xs text-slate-200 font-mono focus:border-indigo-500"
          />
        </div>

        <!-- Live Visual Preview Container -->
        <div id="visual-preview-container" class="rounded-xl border border-slate-800 bg-slate-900/60 p-3 min-h-[140px] flex items-center justify-center text-center">
          <span class="text-xs text-slate-500 font-mono">Nenhum ativo renderizado ainda nesta sessão.</span>
        </div>

        <!-- Gallery of Stored Assets -->
        <div>
          <div class="flex items-center justify-between mb-2">
            <span class="text-[10px] font-mono uppercase text-slate-400">Galeria de Ativos Locais (.factory/visuals/)</span>
            <span id="gallery-count-badge" class="text-[10px] font-mono text-slate-500">0 ativos</span>
          </div>
          <div id="visual-gallery-grid" class="grid grid-cols-2 sm:grid-cols-3 gap-2.5 max-h-48 overflow-y-auto pr-1">
            <div class="col-span-full py-4 text-center text-xs text-slate-500">Carregando galeria visual...</div>
          </div>
        </div>

      </div>

    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  // Wire Event Listeners
  document.getElementById("btn-refresh-studio")?.addEventListener("click", () => loadStudioData());
  document.getElementById("btn-generate-content")?.addEventListener("click", handleGenerateContent);
  document.getElementById("btn-lint-content")?.addEventListener("click", handleLintContent);
  document.getElementById("btn-generate-visual")?.addEventListener("click", handleGenerateVisual);
  document.getElementById("btn-illustrate-coupled")?.addEventListener("click", handleIllustrateCoupled);
  document.getElementById("studio-content-textarea")?.addEventListener("input", updateWordCount);
}

async function loadStudioData() {
  try {
    const [presetsRes, galleryRes] = await Promise.all([
      studioFetch("/api/content/presets").then((r) => r.ok ? r.json() : []),
      studioFetch("/api/visual/gallery").then((r) => r.ok ? r.json() : []),
    ]);

    studioState.presets = presetsRes || [];
    studioState.gallery = galleryRes || [];

    renderPresetsOptions();
    renderGallery();
  } catch (err) {
    console.error("Failed to load studio data:", err);
  }
}

function renderPresetsOptions() {
  const select = document.getElementById("studio-preset-select");
  if (!select || !studioState.presets.length) return;

  const currentVal = select.value;
  select.innerHTML = studioState.presets.map((p) => `
    <option value="${escapeHtml(p.id)}" ${p.id === currentVal ? "selected" : ""}>
      ${escapeHtml(p.name)} (${p.target_length || 200} palavras)
    </option>
  `).join("");
}

function renderGallery() {
  const grid = document.getElementById("visual-gallery-grid");
  const countBadge = document.getElementById("gallery-count-badge");
  if (!grid) return;

  if (countBadge) {
    countBadge.textContent = `${studioState.gallery.length} ativos salvos`;
  }

  if (!studioState.gallery.length) {
    grid.innerHTML = `<div class="col-span-full py-4 text-center text-xs text-slate-500">Nenhum ativo visual salvo ainda no ledger.</div>`;
    return;
  }

  grid.innerHTML = studioState.gallery.map((item) => `
    <div class="rounded-lg border border-slate-800 bg-slate-900/80 p-2 flex flex-col justify-between hover:border-indigo-500/40 transition">
      <div>
        <div class="flex items-center justify-between text-[9px] font-mono text-slate-400 mb-1">
          <span class="rounded bg-indigo-950/60 px-1 py-0.5 text-indigo-400 uppercase">${escapeHtml(item.format || "svg")}</span>
          <span class="text-slate-500">${escapeHtml(item.asset_type || "graphic")}</span>
        </div>
        <div class="text-[11px] font-medium text-slate-200 truncate" title="${escapeHtml(item.title || item.asset_id)}">
          ${escapeHtml(item.title || item.asset_id)}
        </div>
      </div>
      <div class="mt-2 pt-1 border-t border-slate-800/60 flex items-center justify-between text-[10px] text-slate-500">
        <span>${escapeHtml(item.created_at ? item.created_at.slice(0, 10) : "")}</span>
        ${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" class="text-indigo-400 hover:text-indigo-300">Abrir ↗</a>` : ""}
      </div>
    </div>
  `).join("");
}

function updateWordCount() {
  const textarea = document.getElementById("studio-content-textarea");
  const counter = document.getElementById("content-word-count");
  if (!textarea || !counter) return;

  const text = textarea.value.trim();
  const words = text ? text.split(/\s+/).length : 0;
  counter.textContent = `${words} palavras · ${text.length} chars`;
}

async function handleGenerateContent() {
  const topicInput = document.getElementById("studio-topic-input");
  const presetSelect = document.getElementById("studio-preset-select");
  const personaSelect = document.getElementById("studio-persona-select");
  const textarea = document.getElementById("studio-content-textarea");
  const btn = document.getElementById("btn-generate-content");
  const statusPill = document.getElementById("content-status-pill");

  const topic = topicInput?.value.trim();
  if (!topic) {
    alert("Por favor, informe um tópico ou instrução para a geração de conteúdo.");
    return;
  }

  const format = presetSelect?.value || "linkedin_post";
  const persona = personaSelect?.value || "staff_engineer";

  if (btn) btn.disabled = true;
  if (statusPill) {
    statusPill.textContent = "Gerando via Anti-Slop Engine...";
    statusPill.className = "text-[10px] font-mono px-2 py-0.5 rounded bg-purple-950/60 text-purple-300 animate-pulse";
  }

  try {
    const res = await studioFetch("/api/content/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic: topic,
        content_type: format,
        target_audience: persona,
        offline: true,
      }),
    });

    const data = await res.json();
    const contentText = data.final_content || data.content;
    if (res.ok && contentText) {
      if (textarea) {
        textarea.value = contentText;
        updateWordCount();
      }
      if (data.lint_result) {
        displayLintResult(data.lint_result);
      }
      if (statusPill) {
        statusPill.textContent = `Concluído (${data.model_used || "Local $0"})`;
        statusPill.className = "text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950/60 text-emerald-400";
      }
    } else {
      alert(`Falha na geração: ${data.detail || "Erro inesperado"}`);
    }
  } catch (err) {
    alert(`Erro de conexão: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleLintContent() {
  const textarea = document.getElementById("studio-content-textarea");
  const text = textarea?.value.trim();

  if (!text) {
    alert("Insira ou gere algum texto no editor para auditar.");
    return;
  }

  try {
    const res = await studioFetch("/api/content/lint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: text,
      }),
    });

    const data = await res.json();
    if (res.ok) {
      displayLintResult(data);
    } else {
      alert(`Falha no lint: ${data.detail || "Erro de validação"}`);
    }
  } catch (err) {
    alert(`Erro de conexão: ${err.message}`);
  }
}

function displayLintResult(lint) {
  const panel = document.getElementById("studio-lint-panel");
  const badge = document.getElementById("lint-pure-badge");
  const varianceEl = document.getElementById("lint-variance");
  const clichesCountEl = document.getElementById("lint-cliches-count");
  const clichesListEl = document.getElementById("lint-cliches-list");
  const recEl = document.getElementById("lint-recommendations");

  if (!panel || !lint) return;
  panel.classList.remove("hidden");

  const slopScore = lint.slop_score ?? 0;
  const pureScore = Math.max(0, Math.round(100 - slopScore));
  if (badge) {
    badge.textContent = `Pureza: ${pureScore}/100 (${lint.cleanliness_rating || "Puro"})`;
    badge.className = pureScore >= 70
      ? "px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-950/80 text-emerald-300 border border-emerald-700/60"
      : "px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-rose-950/80 text-rose-300 border border-rose-700/60";
  }

  if (varianceEl) {
    const varianceVal = lint.sentence_length_variance ?? lint.sentence_variance ?? "N/A";
    varianceEl.textContent = `${varianceVal} (${lint.cadence_rating || "Variada"})`;
  }

  const cliches = lint.violations || lint.cliches_detected || [];
  if (clichesCountEl) {
    clichesCountEl.textContent = `${lint.violations_count ?? cliches.length} encontrados`;
  }

  if (clichesListEl) {
    if (cliches.length) {
      clichesListEl.innerHTML = cliches.map((c) => {
        const word = typeof c === "string" ? c : (c.word || c.phrase || JSON.stringify(c));
        return `<span class="px-1.5 py-0.5 rounded bg-rose-950/60 border border-rose-800/60 text-rose-300">${escapeHtml(word)}</span>`;
      }).join("");
    } else {
      clichesListEl.innerHTML = `<span class="text-emerald-400">✓ Zero clichês artificiais detectados!</span>`;
    }
  }

  if (recEl) {
    const recs = lint.top_fixes || lint.recommendations || [];
    recEl.textContent = recs.length ? recs.join(" · ") : "Texto em conformidade com as garantias anti-slop da Dark Factory.";
  }
}

async function handleGenerateVisual() {
  const promptInput = document.getElementById("visual-prompt-input");
  const typeSelect = document.getElementById("visual-type-select");
  const ratioSelect = document.getElementById("visual-ratio-select");
  const btn = document.getElementById("btn-generate-visual");
  const preview = document.getElementById("visual-preview-container");

  const prompt = promptInput?.value.trim();
  if (!prompt) {
    alert("Por favor, digite uma descrição ou título visual.");
    return;
  }

  const assetType = typeSelect?.value || "social_banner";
  const ratio = ratioSelect?.value || "16:9";

  if (btn) btn.disabled = true;
  if (preview) {
    preview.innerHTML = `<span class="text-xs text-indigo-400 font-mono animate-pulse">Renderizando ativo procedural context-aware ($0)...</span>`;
  }

  try {
    const res = await studioFetch("/api/visual/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: prompt,
        asset_type: assetType,
        aspect_ratio: ratio,
        theme: "modern_minimalist_dark",
        offline: true,
      }),
    });

    const data = await res.json();
    if (res.ok) {
      renderVisualPreview(data);
      await loadStudioData();
    } else {
      alert(`Falha na renderização visual: ${data.detail || "Erro inesperado"}`);
    }
  } catch (err) {
    alert(`Erro de rede: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleIllustrateCoupled() {
  const textarea = document.getElementById("studio-content-textarea");
  const typeSelect = document.getElementById("visual-type-select");
  const ratioSelect = document.getElementById("visual-ratio-select");
  const btn = document.getElementById("btn-illustrate-coupled");
  const preview = document.getElementById("visual-preview-container");

  const text = textarea?.value.trim();
  if (!text) {
    alert("O editor de texto está vazio! Gere ou digite algum texto antes de disparar a ilustração acoplada.");
    return;
  }

  const assetType = typeSelect?.value || "social_banner";
  const ratio = ratioSelect?.value || "16:9";

  if (btn) btn.disabled = true;
  if (preview) {
    preview.innerHTML = `<span class="text-xs text-indigo-400 font-mono animate-pulse">Analisando metáforas semânticas e renderizando ilustração acoplada...</span>`;
  }

  try {
    const res = await studioFetch("/api/visual/illustrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: text,
        asset_type: assetType,
        aspect_ratio: ratio,
        offline: true,
      }),
    });

    const data = await res.json();
    if (res.ok) {
      renderVisualPreview(data);
      await loadStudioData();
    } else {
      alert(`Falha ao ilustrar texto: ${data.detail || "Erro desconhecido"}`);
    }
  } catch (err) {
    alert(`Erro de conexão: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderVisualPreview(data) {
  const preview = document.getElementById("visual-preview-container");
  if (!preview || !data) return;

  if (data.content_svg) {
    preview.innerHTML = `
      <div class="w-full space-y-2">
        <div class="flex items-center justify-between text-[10px] font-mono text-slate-400 pb-1 border-b border-slate-800">
          <span>Ativo: <strong class="text-indigo-300">${escapeHtml(data.asset_id)}</strong> (${escapeHtml(data.aspect_ratio)})</span>
          <span class="text-emerald-400">✓ Renderizado com Sucesso</span>
        </div>
        <div class="max-h-56 overflow-hidden rounded-lg border border-slate-800 bg-slate-950 flex items-center justify-center p-2">
          ${data.content_svg}
        </div>
      </div>
    `;
  } else if (data.url) {
    preview.innerHTML = `
      <div class="w-full space-y-2">
        <div class="flex items-center justify-between text-[10px] font-mono text-slate-400 pb-1 border-b border-slate-800">
          <span>Ativo: <strong class="text-indigo-300">${escapeHtml(data.asset_id)}</strong></span>
          <a href="${escapeHtml(data.url)}" target="_blank" class="text-indigo-400 hover:text-indigo-300">Abrir original ↗</a>
        </div>
        <img src="${escapeHtml(data.url)}" alt="Ativo Visual Gerado" class="max-h-56 mx-auto rounded-lg border border-slate-800" />
      </div>
    `;
  } else {
    preview.innerHTML = `
      <div class="text-xs text-emerald-400 font-mono">
        Ativo ${escapeHtml(data.asset_id)} gerado com sucesso no ledger visual!
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
