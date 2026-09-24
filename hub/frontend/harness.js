/**
 * On-Demand Validation & Remote Test Harness Monitor (Skill 17 / DH-11) for DarkHub.
 * Enables triggering test suites from the web, managing headless test subagents,
 * querying remote test workers, and rendering distilled test reports with [HARNESS_PASS] markers.
 */

const harnessState = {
  running: false,
  lastReport: null,
  selectedScope: "quick",
  targetPath: "tests/test_hub_coverage.py",
  executionMode: "run-tests", // "run-tests" | "execute"
  workers: [],
};

function getHarnessToken() {
  return window.sessionToken || window.state?.sessionToken || localStorage.getItem("darkhub_session_token") || null;
}

async function harnessFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getHarnessToken();
  if (token) {
    headers.set("X-Hub-Session", token);
  }
  return fetch(url, { ...options, headers });
}

function initHarness() {
  mountHarnessSection();
  loadHarnessWorkers();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initHarness);
} else {
  initHarness();
}

function mountHarnessSection() {
  if (document.getElementById("harness-validation-section")) return;

  const targetAnchor =
    document.getElementById("infrastructure-cards-section") ||
    document.getElementById("enterprise-security-section") ||
    document.getElementById("cross-catalog-section") ||
    document.querySelector("main");
  if (!targetAnchor) return;

  const section = document.createElement("section");
  section.id = "harness-validation-section";
  section.className =
    "space-y-4 rounded-2xl border border-emerald-900/40 bg-slate-900/40 p-4 sm:p-5 shadow-lg shadow-emerald-950/10";
  section.innerHTML = `
    <!-- Header -->
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-slate-800/80 pb-4">
      <div>
        <div class="flex items-center gap-2">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400 text-sm">🧪</span>
          <h2 class="text-base font-semibold text-slate-100">Validação sob Demanda & Harness Remoto (DH-11)</h2>
          <span class="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-400 border border-emerald-500/20 font-mono">Skill 17</span>
        </div>
        <p class="text-xs text-slate-400 mt-1">Disparo de suítes de teste a partir da web com relatório destilado, oráculos determinísticos e isolamento de logs.</p>
      </div>
      <div class="flex items-center gap-2">
        <span id="harness-worker-count" class="text-xs font-mono text-slate-400">Verificando workers...</span>
      </div>
    </div>

    <!-- Execution Form Controls -->
    <div class="rounded-xl border border-slate-800 bg-slate-950/60 p-4 space-y-4">
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Escopo de Execução</label>
          <select id="harness-scope-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-emerald-500">
            <option value="quick">Quick Runner (--quick determinismo)</option>
            <option value="file" selected>Arquivo Específico (Alvo pontual)</option>
            <option value="pattern">Padrão Glob / Heurística</option>
            <option value="all">Suíte Offline Completa do CI</option>
          </select>
        </div>
        
        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Alvo do Teste (Target)</label>
          <input 
            id="harness-target-input" 
            type="text" 
            value="tests/test_hub_coverage.py" 
            placeholder="tests/test_xxx.py"
            class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-emerald-500"
          />
        </div>

        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Motor / Endpoint</label>
          <select id="harness-endpoint-select" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-2.5 py-1.5 text-xs text-slate-200 font-mono focus:border-emerald-500">
            <option value="run-tests">Headless Subagent (/api/harness/run-tests)</option>
            <option value="execute">Distributed Worker (/api/harness/execute)</option>
          </select>
        </div>

        <div>
          <label class="block text-[10px] font-mono uppercase text-slate-400 mb-1">Disparo</label>
          <button 
            id="btn-run-harness-tests" 
            type="button" 
            class="w-full py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium transition shadow-md shadow-emerald-600/30 flex items-center justify-center gap-1.5 active:scale-95 disabled:opacity-50 cursor-pointer">
            <span>⚡ Executar Testes</span>
          </button>
        </div>
      </div>

      <div class="flex items-center gap-4 text-[11px] font-mono text-slate-400 pt-1">
        <label class="flex items-center gap-2 cursor-pointer">
          <input id="harness-failfast-check" type="checkbox" class="rounded border-slate-800 bg-slate-900 text-emerald-600" />
          <span>Parar na primeira falha (-x fail-fast)</span>
        </label>
        <span class="text-slate-600">|</span>
        <div class="flex items-center gap-1.5">
          <span>Timeout:</span>
          <select id="harness-timeout-select" class="bg-slate-900 border border-slate-800 rounded px-1.5 py-0.5 text-[10px] font-mono text-slate-300">
            <option value="30">30s</option>
            <option value="60" selected>60s</option>
            <option value="120">120s</option>
            <option value="300">300s</option>
          </select>
        </div>
      </div>
    </div>

    <!-- Results Container -->
    <div id="harness-results-container" class="hidden rounded-xl border border-slate-800 bg-slate-950/80 p-4 space-y-3 font-mono">
      <!-- Injected dynamically -->
    </div>
  `;

  targetAnchor.parentNode.insertBefore(section, targetAnchor.nextSibling);

  // Wire Event Listeners
  document.getElementById("btn-run-harness-tests")?.addEventListener("click", handleRunTests);
  document.getElementById("harness-scope-select")?.addEventListener("change", handleScopeChange);
}

function handleScopeChange(e) {
  const scope = e.target.value;
  const input = document.getElementById("harness-target-input");
  if (!input) return;

  if (scope === "quick") {
    input.value = "core/harness/runner.py --quick";
  } else if (scope === "file") {
    input.value = "tests/test_hub_coverage.py";
  } else if (scope === "pattern") {
    input.value = "tests/test_hub_*.py";
  } else if (scope === "all") {
    input.value = "tests/";
  }
}

async function loadHarnessWorkers() {
  const label = document.getElementById("harness-worker-count");
  try {
    const res = await harnessFetch("/api/harness/workers");
    if (res.ok) {
      const workers = await res.json();
      harnessState.workers = workers || [];
      if (label) {
        const available = workers.filter((w) => w.status === "available" || w.status === "ready").length;
        label.innerHTML = `Workers Conectados: <strong class="text-emerald-400 font-bold">${available}/${workers.length}</strong>`;
      }
    }
  } catch (err) {
    if (label) label.textContent = "Workers: Modo local ativo ($0)";
  }
}

async function handleRunTests() {
  const scopeSelect = document.getElementById("harness-scope-select");
  const targetInput = document.getElementById("harness-target-input");
  const endpointSelect = document.getElementById("harness-endpoint-select");
  const failfastCheck = document.getElementById("harness-failfast-check");
  const timeoutSelect = document.getElementById("harness-timeout-select");
  const btn = document.getElementById("btn-run-harness-tests");
  const resultsContainer = document.getElementById("harness-results-container");

  const scope = scopeSelect?.value || "file";
  const target = targetInput?.value.trim() || "tests/test_hub_coverage.py";
  const endpointMode = endpointSelect?.value || "run-tests";
  const failFast = !!failfastCheck?.checked;
  const timeout = parseInt(timeoutSelect?.value || "60", 10);

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `
      <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
      </svg>
      <span>Executando...</span>
    `;
  }

  if (resultsContainer) {
    resultsContainer.classList.remove("hidden");
    resultsContainer.innerHTML = `
      <div class="py-6 text-center text-xs text-emerald-400 flex items-center justify-center gap-2">
        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
        <span>Executando suíte via harness determinístico (${escapeHtml(target)})...</span>
      </div>
    `;
  }

  const endpointUrl = endpointMode === "execute" ? "/api/harness/execute" : "/api/harness/run-tests";

  const payload = {
    target: target,
    scope: scope,
    timeout_seconds: timeout,
    fail_fast: failFast,
    worker_mode: "auto",
    allow_fallback: true,
  };

  try {
    const res = await harnessFetch(endpointUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const report = await res.json();
    harnessState.lastReport = report;
    renderDistilledReport(report);
  } catch (err) {
    if (resultsContainer) {
      resultsContainer.innerHTML = `
        <div class="py-4 text-xs text-rose-400">
          <strong>[HARNESS_ERROR]</strong> Falha de comunicação na execução remota: ${escapeHtml(err.message)}
        </div>
      `;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<span>⚡ Executar Testes</span>`;
    }
  }
}

function renderDistilledReport(report) {
  const container = document.getElementById("harness-results-container");
  if (!container || !report) return;

  const isPass = report.success || report.verdict === "PASSED";
  const verdictBadge = isPass
    ? `<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-emerald-950/90 text-emerald-300 border border-emerald-600 tracking-wide">[HARNESS_PASS]</span>`
    : `<span class="px-2.5 py-1 rounded-md text-xs font-bold bg-rose-950/90 text-rose-300 border border-rose-600 tracking-wide">[HARNESS_FAIL]</span>`;

  const durationSec = typeof report.duration_seconds === "number" ? report.duration_seconds.toFixed(2) : "—";

  container.innerHTML = `
    <!-- Top Summary Banner -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-3 border-b border-slate-800 gap-2">
      <div class="flex items-center gap-3">
        ${verdictBadge}
        <span class="text-xs text-slate-200 font-semibold">${escapeHtml(report.concise_summary || "Execução concluída")}</span>
      </div>
      <div class="flex items-center gap-3 text-[11px] text-slate-400">
        <span>Duração: <strong class="text-slate-200">${durationSec}s</strong></span>
        <span>Worker: <strong class="text-slate-200">${escapeHtml(report.worker_id || "local")}</strong></span>
        <span>Modo: <strong class="text-slate-200">${escapeHtml(report.execution_mode || "local")}</strong></span>
      </div>
    </div>

    <!-- Counters -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1 text-xs">
      <div class="p-2 rounded-lg bg-slate-900 border border-slate-800">
        <span class="text-[10px] text-slate-500 uppercase block">Descobertos</span>
        <strong class="text-slate-200">${report.total_discovered || 0}</strong>
      </div>
      <div class="p-2 rounded-lg bg-slate-900 border border-slate-800">
        <span class="text-[10px] text-emerald-500 uppercase block">Aprovados</span>
        <strong class="text-emerald-400 font-bold">${report.passed_count || 0}</strong>
      </div>
      <div class="p-2 rounded-lg bg-slate-900 border border-slate-800">
        <span class="text-[10px] text-rose-500 uppercase block">Falhas</span>
        <strong class="${(report.failed_count || 0) > 0 ? 'text-rose-400 font-bold' : 'text-slate-400'}">${report.failed_count || 0}</strong>
      </div>
      <div class="p-2 rounded-lg bg-slate-900 border border-slate-800">
        <span class="text-[10px] text-slate-500 uppercase block">Ignorados</span>
        <strong class="text-slate-400">${report.skipped_count || 0}</strong>
      </div>
    </div>

    <!-- Agent Feedback -->
    ${report.agent_feedback ? `
      <div class="p-2.5 rounded-lg bg-slate-900/90 border border-slate-800 text-[11px] text-slate-300">
        <div class="text-[10px] uppercase text-slate-500 mb-1">Feedback Destilado para Agentes</div>
        <div>${escapeHtml(report.agent_feedback)}</div>
      </div>
    ` : ""}

    <!-- Failures Detail if any -->
    ${(report.failures && report.failures.length > 0) ? `
      <div class="space-y-2 pt-2">
        <div class="text-xs font-semibold text-rose-400">Falhas Isoladas (${report.failures.length}):</div>
        <div class="space-y-1.5 max-h-56 overflow-y-auto pr-1">
          ${report.failures.map((f) => `
            <div class="p-2 rounded-lg bg-rose-950/30 border border-rose-900/50 text-[11px] space-y-1">
              <div class="flex items-center justify-between text-rose-300">
                <span class="font-bold truncate max-w-md">${escapeHtml(f.test_id)}</span>
                <span class="text-[10px] text-slate-400">${escapeHtml(f.file)}${f.line ? `:${f.line}` : ""}</span>
              </div>
              <div class="text-slate-300 text-[10px]">${escapeHtml(f.error_message || f.error_type)}</div>
              ${f.snippet ? `<pre class="p-1.5 rounded bg-slate-950 text-slate-400 text-[10px] overflow-x-auto">${escapeHtml(f.snippet)}</pre>` : ""}
            </div>
          `).join("")}
        </div>
      </div>
    ` : ""}

    <!-- Raw Log Path on disk -->
    ${report.raw_log_path ? `
      <div class="text-[10px] text-slate-500 pt-1 flex items-center justify-between border-t border-slate-800/60">
        <span>Log bruto isolado em disco: <code class="text-slate-400">${escapeHtml(report.raw_log_path)}</code></span>
        <span>Exit code: ${report.exit_code}</span>
      </div>
    ` : ""}
  `;
}

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
