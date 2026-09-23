/**
 * DarkHub - Learning Packs & Cognitive Uplift (DH-06 / USR-53)
 *
 * Provides read-only consultation for Session Learning Packs,
 * multi-level Feynman explanations, analogical anchors, skeptic shields,
 * Anki flashcards, and Segundo Cérebro perpetual knowledge.
 */

const learningUi = {
  activeTab: "latest",
  latestPack: null,
  selectedPack: null,
  packs: [],
  loading: false,
};

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function openLearningDrawer() {
  const drawer = document.getElementById("learning-drawer");
  if (!drawer) return;
  drawer.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  loadLatestLearningPack();
  loadLearningPacks();
}

function closeLearningDrawer() {
  const drawer = document.getElementById("learning-drawer");
  if (!drawer) return;
  drawer.classList.add("hidden");
  document.body.style.overflow = "";
}

function switchLearningTab(tab) {
  learningUi.activeTab = tab;
  const tabs = ["latest", "history", "flashcards", "knowledge"];
  tabs.forEach((t) => {
    const btn = document.getElementById(`learn-tab-${t}`);
    const view = document.getElementById(`learn-view-${t}`);
    const isActive = t === tab;
    if (btn) {
      btn.className = isActive
        ? "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all bg-amber-500/20 text-amber-300 border border-amber-500/40 font-semibold"
        : "px-3 py-1.5 rounded-xl text-xs font-mono font-medium transition-all text-slate-400 hover:text-slate-200 border border-transparent";
      btn.setAttribute("aria-selected", String(isActive));
    }
    if (view) {
      view.classList.toggle("hidden", !isActive);
    }
  });

  if (tab === "latest" && !learningUi.latestPack) {
    loadLatestLearningPack();
  } else if (tab === "history" && !learningUi.packs.length) {
    loadLearningPacks();
  } else if (tab === "flashcards") {
    renderFlashcardsTab();
  } else if (tab === "knowledge") {
    renderKnowledgeTab();
  }
}

async function loadLatestLearningPack() {
  const container = document.getElementById("learn-latest-content");
  if (container) {
    container.innerHTML = '<div class="py-12 text-center text-slate-500 text-xs font-mono">Carregando o último Learning Pack...</div>';
  }

  try {
    const res = await fetch("/api/learning-packs/latest");
    if (!res.ok) {
      throw new Error(res.status === 404 ? "Nenhum Learning Pack gerado ainda." : "Falha ao carregar Learning Pack");
    }
    const pack = await res.json();
    learningUi.latestPack = pack;
    learningUi.selectedPack = pack;
    renderLearningPack(pack, "learn-latest-content");
  } catch (err) {
    if (container) {
      container.innerHTML = `
        <div class="p-6 rounded-2xl border border-slate-800 bg-slate-950/40 text-center space-y-2">
          <div class="text-amber-400 text-sm font-semibold">${escapeHtml(err.message)}</div>
          <p class="text-xs text-slate-500 font-mono">Os Learning Packs são sintetizados ao término de sessões e entregas da fábrica.</p>
        </div>
      `;
    }
  }
}

async function loadLearningPacks() {
  const container = document.getElementById("learn-history-list");
  if (container) {
    container.innerHTML = '<div class="py-8 text-center text-slate-500 text-xs font-mono">Carregando histórico de packs...</div>';
  }

  try {
    const res = await fetch("/api/learning-packs");
    if (!res.ok) throw new Error("Falha ao listar Learning Packs");
    const packs = await res.json();
    learningUi.packs = packs;
    renderLearningPacksHistory(packs);

    const countBadge = document.getElementById("learn-history-count");
    if (countBadge) {
      countBadge.textContent = String(packs.length);
    }
  } catch (err) {
    if (container) {
      container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${escapeHtml(err.message)}</div>`;
    }
  }
}

async function inspectLearningPack(packId) {
  const container = document.getElementById("learn-latest-content");
  if (container) {
    container.innerHTML = '<div class="py-12 text-center text-slate-500 text-xs font-mono">Carregando Learning Pack...</div>';
  }
  switchLearningTab("latest");

  try {
    const res = await fetch(`/api/learning-packs/${encodeURIComponent(packId)}`);
    if (!res.ok) throw new Error("Falha ao carregar o pack selecionado");
    const pack = await res.json();
    learningUi.selectedPack = pack;
    renderLearningPack(pack, "learn-latest-content");
  } catch (err) {
    if (container) {
      container.innerHTML = `<div class="p-4 rounded-xl border border-rose-500/30 bg-rose-950/20 text-rose-300 text-xs font-mono">Erro: ${escapeHtml(err.message)}</div>`;
    }
  }
}

function openLearningPackHtml(packId) {
  const id = packId || (learningUi.selectedPack ? learningUi.selectedPack.pack_id : null);
  if (!id) return;
  window.open(`/api/learning-packs/${encodeURIComponent(id)}/html`, "_blank");
}

function exportLearningPackAnki(packId) {
  const id = packId || (learningUi.selectedPack ? learningUi.selectedPack.pack_id : null);
  if (!id) return;
  const link = document.createElement("a");
  link.href = `/api/learning-packs/${encodeURIComponent(id)}/export-anki`;
  link.download = `${id}_anki.tsv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function renderLearningPacksHistory(packs) {
  const container = document.getElementById("learn-history-list");
  if (!container) return;

  if (!packs || !packs.length) {
    container.innerHTML = '<div class="py-12 text-center text-slate-500 text-xs font-mono">Nenhum pack encontrado no ledger.</div>';
    return;
  }

  container.innerHTML = `
    <div class="space-y-3">
      ${packs.map((p) => `
        <div class="p-4 rounded-2xl border border-slate-800 bg-slate-950/60 hover:border-slate-700 transition-all flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div class="space-y-1 flex-1">
            <div class="flex items-center gap-2 flex-wrap">
              <h4 class="text-sm font-bold text-white">${escapeHtml(p.title || p.pack_id)}</h4>
              <span class="px-2 py-0.5 rounded-full bg-slate-900 border border-slate-800 text-[10px] font-mono text-slate-400">
                ${escapeHtml(p.timestamp ? p.timestamp.slice(0, 10) : "")}
              </span>
              <span class="px-2 py-0.5 rounded-full bg-amber-500/10 border border-amber-500/30 text-[10px] font-mono text-amber-300">
                ${p.concepts_count || 0} conceitos
              </span>
              <span class="px-2 py-0.5 rounded-full bg-cyan-500/10 border border-cyan-500/30 text-[10px] font-mono text-cyan-300">
                ${p.flashcards_count || 0} flashcards
              </span>
            </div>
            <p class="text-xs text-slate-400 line-clamp-2">${escapeHtml(p.executive_summary || "")}</p>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onclick="inspectLearningPack('${escapeHtml(p.pack_id)}')"
              class="px-3 py-1.5 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-200 border border-slate-700 text-xs font-medium transition-all">
              Ver Pack
            </button>
            <button
              type="button"
              onclick="openLearningPackHtml('${escapeHtml(p.pack_id)}')"
              title="Abrir Widget HTML Interativo"
              class="px-3 py-1.5 rounded-xl bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 text-xs font-medium transition-all">
              HTML ↗
            </button>
            <button
              type="button"
              onclick="exportLearningPackAnki('${escapeHtml(p.pack_id)}')"
              title="Baixar Deck Anki TSV"
              class="px-3 py-1.5 rounded-xl bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs font-medium transition-all">
              Anki ↓
            </button>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

function renderLearningPack(pack, containerId) {
  const container = document.getElementById(containerId);
  if (!container || !pack) return;

  const concepts = pack.concepts || [];
  const flashcards = pack.flashcards || [];

  container.innerHTML = `
    <div class="space-y-6">
      <!-- Pack Header Card -->
      <div class="p-6 rounded-2xl border border-amber-500/30 bg-gradient-to-b from-slate-900/80 to-slate-950/80 space-y-4">
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <span class="px-2.5 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/30 text-[10px] font-mono text-amber-300 font-semibold uppercase">
                Learning Pack
              </span>
              <span class="text-xs text-slate-500 font-mono">${escapeHtml(pack.pack_id)}</span>
            </div>
            <h3 class="text-lg font-bold text-white mt-1">${escapeHtml(pack.title)}</h3>
            <p class="text-xs text-slate-400 mt-0.5">Sessão: <code class="text-slate-300 font-mono">${escapeHtml(pack.session_id || "darkfac")}</code> · Registrado em: ${escapeHtml(pack.timestamp ? pack.timestamp.replace("T", " ").slice(0, 19) : "--")}</p>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              onclick="openLearningPackHtml('${escapeHtml(pack.pack_id)}')"
              class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs shadow-lg shadow-indigo-600/30 transition-all flex items-center gap-1.5">
              <span>🌐</span>
              <span>Abrir Widget HTML</span>
            </button>
            <button
              type="button"
              onclick="exportLearningPackAnki('${escapeHtml(pack.pack_id)}')"
              class="px-4 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-200 border border-slate-700 font-medium text-xs transition-all flex items-center gap-1.5">
              <span>📇</span>
              <span>Baixar Deck Anki (.tsv)</span>
            </button>
          </div>
        </div>

        <!-- Executive Summary -->
        <div class="p-4 rounded-xl border border-slate-800 bg-slate-950/60 space-y-1">
          <div class="text-[10px] uppercase font-mono tracking-wider text-slate-500 font-semibold">Resumo Executivo (Pitch de 30s)</div>
          <p class="text-xs text-slate-300 leading-relaxed">${escapeHtml(pack.executive_summary)}</p>
        </div>

        <!-- Stats Bar -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
          <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
            <span class="text-slate-500 block text-[10px] uppercase">Conceitos Mapeados</span>
            <span class="text-amber-300 font-bold text-sm">${concepts.length}</span>
          </div>
          <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
            <span class="text-slate-500 block text-[10px] uppercase">Flashcards Gerados</span>
            <span class="text-cyan-300 font-bold text-sm">${flashcards.length}</span>
          </div>
          <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
            <span class="text-slate-500 block text-[10px] uppercase">Arquivos Analisados</span>
            <span class="text-slate-300 font-bold text-sm">${pack.files_analyzed ? pack.files_analyzed.length : 0}</span>
          </div>
          <div class="p-2.5 rounded-xl bg-slate-950 border border-slate-800/80">
            <span class="text-slate-500 block text-[10px] uppercase">Garantia Anti-Alucinação</span>
            <span class="text-emerald-400 font-bold text-sm">Fail-Closed 100%</span>
          </div>
        </div>
      </div>

      <!-- Concepts Detailed Section -->
      <div class="space-y-4">
        <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 font-mono">
          Conceitos Arquiteturais & Níveis Feynman
        </h4>
        
        <div class="space-y-4">
          ${concepts.map((concept, idx) => {
            const feynman = concept.tiers || concept.feynman_levels || {};
            const pitch = feynman.pitch_30s || "--";
            const staff = feynman.staff_architect || feynman.staff_plus_defense || "--";
            const underTheHood = feynman.under_the_hood || "--";
            const anchor = concept.analogical_anchor || {};
            const mentalAnchor = concept.mental_anchor || anchor.mental_model || "--";
            const metaphor = anchor.everyday_metaphor || "";
            const antiPattern = anchor.anti_pattern || "";

            const defenses = Array.isArray(concept.defense) ? concept.defense : (concept.skeptic_shield ? [concept.skeptic_shield] : []);
            const firstDefense = defenses.length ? defenses[0] : {};
            const objection = firstDefense.question || firstDefense.question_or_objection || "--";
            const rebuttal = firstDefense.bulletproof_answer || firstDefense.rebuttal || "--";
            const source = firstDefense.context || firstDefense.verifiable_source || "";

            return `
              <div class="p-5 rounded-2xl border border-slate-800 bg-slate-950/60 space-y-4">
                <div class="flex items-center justify-between border-b border-slate-800 pb-3">
                  <div class="flex items-center gap-2.5">
                    <span class="w-6 h-6 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/30 flex items-center justify-center text-xs font-mono font-bold">${idx + 1}</span>
                    <h5 class="text-sm font-bold text-white">${escapeHtml(concept.name)}</h5>
                  </div>
                  <span class="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 text-[10px] font-mono text-slate-400">
                    ${escapeHtml(concept.category || "Cognitive Unit")}
                  </span>
                </div>

                <!-- 3 Feynman Levels -->
                <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                  <div class="p-3 rounded-xl border border-slate-800 bg-slate-900/40 space-y-1">
                    <div class="text-[10px] font-mono uppercase text-amber-400 font-semibold">1. Pitch de 30s (Executivo/Cliente)</div>
                    <p class="text-xs text-slate-300 leading-relaxed">${escapeHtml(pitch)}</p>
                  </div>
                  <div class="p-3 rounded-xl border border-slate-800 bg-slate-900/40 space-y-1">
                    <div class="text-[10px] font-mono uppercase text-indigo-400 font-semibold">2. Defesa Staff+ (Arquitetura)</div>
                    <p class="text-xs text-slate-300 leading-relaxed">${escapeHtml(staff)}</p>
                  </div>
                  <div class="p-3 rounded-xl border border-slate-800 bg-slate-900/40 space-y-1">
                    <div class="text-[10px] font-mono uppercase text-cyan-400 font-semibold">3. Mecânica Sob o Capô</div>
                    <p class="text-xs text-slate-300 leading-relaxed font-mono text-[11px]">${escapeHtml(underTheHood)}</p>
                  </div>
                </div>

                <!-- Analogical Anchor & Skeptic Shield -->
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2">
                  <!-- Anchor -->
                  <div class="p-3.5 rounded-xl border border-slate-800/80 bg-slate-900/20 space-y-2">
                    <div class="text-[10px] font-mono uppercase text-emerald-400 font-semibold flex items-center gap-1.5">
                      <span>⚓</span>
                      <span>Âncora Mental Analógica</span>
                    </div>
                    <div class="space-y-1.5 text-xs text-slate-300">
                      <div><strong class="text-slate-400 text-[11px]">Modelo Mental:</strong> ${escapeHtml(mentalAnchor)}</div>
                      ${metaphor ? `<div><strong class="text-slate-400 text-[11px]">Metáfora Cotidiana:</strong> ${escapeHtml(metaphor)}</div>` : ""}
                      ${antiPattern ? `<div><strong class="text-rose-400 text-[11px]">Anti-pattern:</strong> ${escapeHtml(antiPattern)}</div>` : ""}
                    </div>
                  </div>

                  <!-- Skeptic Shield -->
                  <div class="p-3.5 rounded-xl border border-slate-800/80 bg-slate-900/20 space-y-2">
                    <div class="text-[10px] font-mono uppercase text-purple-400 font-semibold flex items-center gap-1.5">
                      <span>🛡️</span>
                      <span>Escudo Contra Céticos</span>
                    </div>
                    <div class="space-y-1.5 text-xs text-slate-300">
                      <div><strong class="text-slate-400 text-[11px]">Objeção Típica:</strong> <em>"${escapeHtml(objection)}"</em></div>
                      <div><strong class="text-slate-400 text-[11px]">Refutação Técnica:</strong> ${escapeHtml(rebuttal)}</div>
                      ${source ? `<div><strong class="text-slate-400 text-[11px]">Fonte Verificável:</strong> <code class="text-[10px] text-purple-300">${escapeHtml(source)}</code></div>` : ""}
                    </div>
                  </div>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      </div>
    </div>
  `;
}

function renderFlashcardsTab() {
  const container = document.getElementById("learn-flashcards-container");
  if (!container) return;

  const pack = learningUi.selectedPack || learningUi.latestPack;
  if (!pack || !pack.flashcards || !pack.flashcards.length) {
    container.innerHTML = '<div class="py-12 text-center text-slate-500 text-xs font-mono">Nenhum flashcard disponível no pack ativo.</div>';
    return;
  }

  const flashcards = pack.flashcards;

  container.innerHTML = `
    <div class="space-y-4">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div>
          <h4 class="text-xs font-bold text-white uppercase font-mono">Baralho de Repetição Espaçada</h4>
          <p class="text-[11px] text-slate-400 font-mono">Flashcards otimizados para exportação Anki e memorização ativa das decisões de código.</p>
        </div>
        <button
          type="button"
          onclick="exportLearningPackAnki('${escapeHtml(pack.pack_id)}')"
          class="px-3.5 py-1.5 rounded-xl bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs font-medium transition-all">
          Baixar TSV (${flashcards.length}) ↓
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        ${flashcards.map((card, idx) => {
          const front = card.front_prompt || card.front || "--";
          const back = card.back_solution || card.back || "--";
          const tag = (Array.isArray(card.tags) && card.tags.length ? card.tags[0] : card.tag) || "darkfac";

          return `
            <div class="p-4 rounded-xl border border-slate-800 bg-slate-950/60 flex flex-col justify-between space-y-3">
              <div class="space-y-2">
                <div class="flex items-center justify-between text-[10px] font-mono text-slate-500 border-b border-slate-900 pb-1.5">
                  <span class="text-cyan-400 font-bold">Card #${idx + 1}</span>
                  <span class="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 text-slate-400">${escapeHtml(tag)}</span>
                </div>
                <div class="text-xs font-semibold text-slate-200">
                  <span class="text-amber-400 font-mono text-[10px] block uppercase">Pergunta (Frente):</span>
                  ${escapeHtml(front)}
                </div>
              </div>
              <div class="p-3 rounded-lg bg-slate-900/60 border border-slate-800 text-xs text-slate-300 space-y-1">
                <span class="text-emerald-400 font-mono text-[10px] block uppercase">Resposta (Verso):</span>
                <p class="leading-relaxed">${escapeHtml(back)}</p>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function renderKnowledgeTab() {
  const container = document.getElementById("learn-knowledge-container");
  if (!container) return;

  container.innerHTML = `
    <div class="space-y-4 font-mono">
      <div class="p-5 rounded-2xl border border-slate-800 bg-slate-950/50 space-y-3">
        <div class="flex items-center justify-between">
          <div>
            <h4 class="text-xs font-bold text-white uppercase">Segundo Cérebro & Base de Conhecimento</h4>
            <p class="text-[11px] text-slate-400 mt-0.5">Memória perpétua da fábrica, papers científicos e referências técnicas com isolamento estrito multi-projeto.</p>
          </div>
          <span class="px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-[10px] text-emerald-300 font-semibold">
            Fail-Closed Anti-Alucinação
          </span>
        </div>
        
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-2 text-xs">
          <div class="p-3 rounded-xl border border-slate-800 bg-slate-950">
            <span class="text-slate-500 block text-[10px] uppercase">Domínios Conectados</span>
            <span class="text-slate-200 font-bold mt-0.5 block">darkfac · atrium · jarvis</span>
          </div>
          <div class="p-3 rounded-xl border border-slate-800 bg-slate-950">
            <span class="text-slate-500 block text-[10px] uppercase">Citação Verificável</span>
            <span class="text-slate-200 font-bold mt-0.5 block">SHA-256 + Offset de Página</span>
          </div>
          <div class="p-3 rounded-xl border border-slate-800 bg-slate-950">
            <span class="text-slate-500 block text-[10px] uppercase">Modo de Operação</span>
            <span class="text-amber-300 font-bold mt-0.5 block">Consulta Somente-Leitura</span>
          </div>
        </div>
      </div>

      <div class="p-4 rounded-xl border border-slate-800 bg-slate-950/30 text-xs text-slate-400 space-y-2">
        <div class="text-slate-300 font-semibold uppercase text-[11px]">Como o Segundo Cérebro opera:</div>
        <p class="leading-relaxed">1. Todo Learning Pack produzido é indexado no Knowledge Ledger da Dark Factory.</p>
        <p class="leading-relaxed">2. Provedores externos e papers (arXiv, GitHub repos) são catalogados com hash criptográfico.</p>
        <p class="leading-relaxed">3. Agentes e harnesses consultam a base via chamadas headless programáticas em <code class="text-slate-200">core/knowledge</code> e <code class="text-slate-200">core/research</code>.</p>
      </div>
    </div>
  `;
}
