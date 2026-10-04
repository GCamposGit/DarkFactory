/* Esteira ao vivo — USR-138. JS vanilla (ES2020), sem build, sem dependências externas.
 * Todo dado vindo da API entra no DOM via textContent / setAttribute (nunca innerHTML).
 * innerHTML é usado apenas para os ícones SVG constantes definidos neste arquivo. */
(function (root) {
  'use strict';

  /* =====================================================================
   * 1. Funções puras (expostas em window.LineLive para teste)
   * ===================================================================== */

  const DEFAULT_STAGE_ORDER = ['grill', 'planning', 'development', 'validation', 'independent_review', 'integration', 'build_deploy', 'retrospective'];
  const SHORT_LABELS = {
    grill: 'Grill', planning: 'Plano', development: 'Dev', validation: 'Validação',
    independent_review: 'Revisão', integration: 'Integração', build_deploy: 'Deploy',
    retrospective: 'Retro', target_journey: 'Jornada',
  };
  const STAGE_STATUS = {
    not_reached: { key: 'not_reached', tone: 'idle', icon: null, text: 'Não alcançada', sub: '' },
    pending: { key: 'pending', tone: 'pending', icon: 'clock', text: 'Pendente', sub: 'pendente' },
    running: { key: 'running', tone: 'running', icon: 'play', text: 'Rodando', sub: '' },
    succeeded: { key: 'succeeded', tone: 'success', icon: 'check', text: 'Concluída', sub: '' },
    retry: { key: 'warn', tone: 'warn', icon: 'retry', text: 'Nova tentativa', sub: 'retry' },
    replan: { key: 'warn', tone: 'warn', icon: 'retry', text: 'Replanejando', sub: 'replan' },
    waiting_dependency: { key: 'human', tone: 'human', icon: 'pause', text: 'Aguardando dependência', sub: 'dependência' },
    waiting_human: { key: 'human', tone: 'human', icon: 'hand', text: 'Aguardando você', sub: 'aguardando' },
    cancelled: { key: 'muted', tone: 'muted', icon: 'ban', text: 'Cancelada', sub: 'cancelada' },
    failed: { key: 'danger', tone: 'danger', icon: 'x', text: 'Falhou', sub: 'falhou' },
  };
  const RUN_STATE = {
    attention: { tone: 'attention', icon: 'hand', text: 'Precisa de você' },
    running: { tone: 'running', icon: 'play', text: 'Rodando' },
    queued: { tone: 'queued', icon: 'clock', text: 'Na fila' },
    succeeded: { tone: 'success', icon: 'check', text: 'Concluído' },
    failed: { tone: 'danger', icon: 'x', text: 'Falhou' },
    cancelled: { tone: 'muted', icon: 'ban', text: 'Cancelado' },
  };
  const ACTIVE_RANK = { attention: 0, running: 1, queued: 2 };

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  /** Converte ISO/epoch em ms. ISO sem fuso é tratado como UTC. */
  function parseTime(v) {
    if (v == null || v === '') return NaN;
    if (typeof v === 'number') return v > 1e12 ? v : v * 1000;
    let s = String(v).trim();
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = s.replace(' ', 'T') + 'Z';
    return Date.parse(s);
  }

  /** 45 s · 3 min 05 s · 12 min · 2 h 14 min · 3 d 4 h */
  function formatDuration(sec) {
    if (sec == null || sec === '' || !isFinite(sec)) return '—';
    const s = Math.max(0, Math.round(Number(sec)));
    if (s < 60) return s + ' s';
    if (s < 3600) {
      const m = Math.floor(s / 60), r = s % 60;
      return r && m < 10 ? m + ' min ' + pad2(r) + ' s' : m + ' min';
    }
    if (s < 86400) {
      const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      return m ? hh + ' h ' + m + ' min' : hh + ' h';
    }
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600);
    return hh ? d + ' d ' + hh + ' h' : d + ' d';
  }

  /** Cronômetro: mm:ss (ou h:mm:ss a partir de 1 h). */
  function formatClock(sec) {
    if (sec == null || !isFinite(sec)) return '–:––';
    const s = Math.max(0, Math.floor(Number(sec)));
    const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return hh ? hh + ':' + pad2(m) + ':' + pad2(r) : pad2(m) + ':' + pad2(r);
  }

  /** "há 3 s" / "há 5 min" / "há 2 h" / "há 3 d" (ts em ms). */
  function formatAge(ts, now) {
    if (!isFinite(ts)) return '—';
    const d = Math.max(0, ((now == null ? Date.now() : now) - ts) / 1000);
    if (d < 1) return 'agora';
    if (d < 60) return 'há ' + Math.floor(d) + ' s';
    if (d < 3600) return 'há ' + Math.floor(d / 60) + ' min';
    if (d < 86400) return 'há ' + Math.floor(d / 3600) + ' h';
    return 'há ' + Math.floor(d / 86400) + ' d';
  }

  /** "em 4 min" / "expirou há 20 s" (ts em ms). */
  function formatUntil(ts, now) {
    if (!isFinite(ts)) return '—';
    const diff = (ts - (now == null ? Date.now() : now)) / 1000;
    return diff >= 0 ? 'em ' + formatDuration(diff) : 'expirou há ' + formatDuration(-diff);
  }

  function formatMoney(v) {
    if (v == null || !isFinite(v)) return '—';
    const n = Number(v);
    const digits = n > 0 && n < 0.01 ? 4 : 2;
    return 'US$ ' + n.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function formatClockTime(ms) {
    if (!isFinite(ms)) return '—';
    const d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function formatDateTime(ms) {
    if (!isFinite(ms)) return '—';
    const d = new Date(ms);
    return pad2(d.getDate()) + '/' + pad2(d.getMonth() + 1) + ' ' + formatClockTime(ms);
  }

  /** Visual de um estágio: { key (classe st-*), tone, icon, text, sub }. */
  function stageVisual(status) {
    const v = STAGE_STATUS[status];
    if (v) return v;
    return { key: 'pending', tone: 'pending', icon: 'clock', text: status ? String(status) : 'Desconhecido', sub: '' };
  }

  function runStateVisual(state) {
    return RUN_STATE[state] || { tone: 'muted', icon: 'ban', text: state ? String(state) : 'Desconhecido' };
  }

  /** Cor determinística por project_id. */
  function projectColor(id) {
    const s = String(id == null ? '' : id);
    let hsh = 2166136261;
    for (let i = 0; i < s.length; i++) { hsh ^= s.charCodeAt(i); hsh = Math.imul(hsh, 16777619) >>> 0; }
    const hue = hsh % 360;
    return {
      hue: hue,
      fg: 'hsl(' + hue + ' 85% 72%)',
      bg: 'hsl(' + hue + ' 60% 50% / .16)',
      border: 'hsl(' + hue + ' 60% 55% / .45)',
    };
  }

  function stageKeys(snapshot, run) {
    const order = snapshot && Array.isArray(snapshot.stage_order) && snapshot.stage_order.length ? snapshot.stage_order : DEFAULT_STAGE_ORDER;
    const extras = ((run && run.stages) || []).map(function (s) { return s.stage; }).filter(function (k) { return order.indexOf(k) < 0; });
    return order.concat(extras.filter(function (k, i) { return extras.indexOf(k) === i; }));
  }

  function stageMap(run) {
    const by = Object.create(null);
    ((run && run.stages) || []).forEach(function (s) { by[s.stage] = s; });
    return by;
  }

  function stageLabel(snapshot, key, stage) {
    return (snapshot && snapshot.stage_labels && snapshot.stage_labels[key]) || (stage && stage.label) || key;
  }

  /** Deriva a visão (filtrada/ordenada) a partir do snapshot. Pura. */
  function deriveView(snapshot, filters) {
    const f = Object.assign({ project: 'all', q: '', showDone: true }, filters || {});
    const snap = snapshot || {};
    const runs = Array.isArray(snap.runs) ? snap.runs : [];
    const events = Array.isArray(snap.events) ? snap.events : [];
    const off = Array.isArray(snap.off_line) ? snap.off_line : [];
    const backlog = Array.isArray(snap.backlog) ? snap.backlog : [];
    const q = String(f.q || '').trim().toLowerCase();
    const okProject = function (pid) { return !f.project || f.project === 'all' || pid === f.project; };
    const okQuery = function () {
      if (!q) return true;
      for (let i = 0; i < arguments.length; i++) {
        const p = arguments[i];
        if (p != null && String(p).toLowerCase().indexOf(q) >= 0) return true;
      }
      return false;
    };

    const counts = new Map();
    const bump = function (pid) { if (pid) counts.set(pid, (counts.get(pid) || 0) + 1); };
    runs.forEach(function (r) { bump(r.project_id); });
    off.forEach(function (t) { bump(t.project_id); });
    backlog.forEach(function (b) { if (b && b.project_id && !counts.has(b.project_id)) counts.set(b.project_id, 0); });
    const projects = Array.from(counts, function (e) { return { id: e[0], count: e[1] }; })
      .sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });

    const filtered = runs.filter(function (r) { return okProject(r.project_id) && okQuery(r.ticket_id, r.run_id, r.demand_id, r.title); });
    const isActive = function (r) { return ACTIVE_RANK[r.state] !== undefined; };
    const active = filtered.filter(isActive)
      .map(function (r, i) { return [r, i]; })
      .sort(function (a, b) { return (ACTIVE_RANK[a[0].state] - ACTIVE_RANK[b[0].state]) || (a[1] - b[1]); })
      .map(function (e) { return e[0]; });
    const done = filtered.filter(function (r) { return !isActive(r); })
      .sort(function (a, b) {
        const ta = parseTime(a.completed_at || a.updated_at), tb = parseTime(b.completed_at || b.updated_at);
        return (isFinite(tb) ? tb : 0) - (isFinite(ta) ? ta : 0);
      });

    const attn = active.filter(function (r) { return r.state === 'attention'; })
      .sort(function (a, b) {
        const ta = parseTime(a.attention && a.attention.since), tb = parseTime(b.attention && b.attention.since);
        return (isFinite(ta) ? ta : Infinity) - (isFinite(tb) ? tb : Infinity);
      })
      .map(function (r) { return { run: r, kind: 'attention' }; });
    const stalled = active.filter(function (r) { return r.stalled && r.state !== 'attention'; })
      .map(function (r) { return { run: r, kind: 'stalled' }; });
    const attention = attn.concat(stalled);

    // Contagem global (sem filtro) para título da aba e contador acessível.
    const attentionAll = runs.filter(function (r) { return r.state === 'attention' || (r.stalled && ACTIVE_RANK[r.state] !== undefined); }).length;

    return {
      projects: projects,
      totalCount: runs.length + off.length,
      active: active,
      attention: attention,
      attentionAll: attentionAll,
      done: done,
      events: events.filter(function (e) { return okProject(e.project_id) && okQuery(e.ticket_id, e.title, e.message); }),
      backlog: backlog.filter(function (b) { return okProject(b.project_id); }),
      offLine: off.filter(function (t) { return okProject(t.project_id) && okQuery(t.id, t.title); }),
      hiddenByFilter: runs.length - filtered.length,
      filtersActive: (f.project && f.project !== 'all') || !!q,
    };
  }

  const api = {
    formatDuration: formatDuration, formatClock: formatClock, formatAge: formatAge, formatUntil: formatUntil,
    formatMoney: formatMoney, formatClockTime: formatClockTime, formatDateTime: formatDateTime,
    stageVisual: stageVisual, runStateVisual: runStateVisual, projectColor: projectColor,
    parseTime: parseTime, deriveView: deriveView, stageKeys: stageKeys,
  };
  root.LineLive = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  if (typeof document === 'undefined' || !document.getElementById('line-live-board')) return;

  /* =====================================================================
   * 2. DOM helpers
   * ===================================================================== */

  const SNAPSHOT_URL = '/api/line/live';
  const STREAM_URL = '/api/line/live/stream';
  const STORE_KEY = 'darkfac.lineLive.v1';
  const POLL_MS = 10000;
  const FETCH_TIMEOUT_MS = 8000;
  const SSE_RETRY_MS = 60000;
  const DONE_PAGE = 8;
  const STALE_LIVE_MS = 120000;

  const $ = function (id) { return document.getElementById(id); };

  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'style') el.style.cssText = v;
        else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (let i = 2; i < arguments.length; i++) appendKids(el, arguments[i]);
    return el;
  }
  function appendKids(el, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { appendKids(el, k); }); return; }
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  function setText(el, t) { if (el.textContent !== t) el.textContent = t; }
  function setClass(el, c) { if (el.className !== c) el.className = c; }

  // Ícones: strings SVG constantes (nunca dados da API).
  const ICONS = {
    check: '<path d="M20 6 9 17l-5-5"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    retry: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pause: '<path d="M9 5v14M15 5v14"/>',
    hand: '<path d="M18 11V6a2 2 0 0 0-4 0v5"/><path d="M14 10V4a2 2 0 0 0-4 0v6.5"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-6-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    play: '<path d="M8 5v14l11-7z" fill="currentColor"/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    flag: '<path d="M4 22V4M4 4h13l-2 4 2 4H4"/>',
  };
  const EVENT_KINDS = {
    stage_started: { icon: 'play', tone: 'info', label: 'Etapa iniciada' },
    stage_succeeded: { icon: 'check', tone: 'success', label: 'Etapa concluída' },
    stage_failed: { icon: 'x', tone: 'danger', label: 'Etapa falhou' },
    waiting_human: { icon: 'hand', tone: 'human', label: 'Aguardando você' },
    retry: { icon: 'retry', tone: 'warn', label: 'Nova tentativa' },
    run_completed: { icon: 'flag', tone: 'success', label: 'Run concluído' },
  };
  function icon(name) {
    const span = h('span', { class: 'icon', 'aria-hidden': 'true' });
    if (ICONS[name]) {
      span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</svg>';
    }
    return span;
  }

  function liveEl(tag, kind, ts, cls, prefix) {
    const el = h(tag, { class: cls || null, 'data-live': kind, 'data-ts': String(ts), 'data-prefix': prefix || null });
    liveRefresh(el, Date.now());
    return el;
  }
  function liveRefresh(el, now) {
    const ts = Number(el.dataset.ts);
    if (!isFinite(ts)) return;
    const kind = el.dataset.live;
    let t;
    if (kind === 'timer') t = formatClock((now - ts) / 1000);
    else if (kind === 'age') t = formatAge(ts, now);
    else if (kind === 'elapsed') t = (el.dataset.prefix || '') + formatDuration((now - ts) / 1000);
    else if (kind === 'until') t = formatUntil(ts, now);
    else return;
    setText(el, t);
  }

  function applyBadge(el, projectId) {
    const c = projectColor(projectId);
    setClass(el, 'proj-badge');
    setText(el, projectId || '—');
    el.style.color = c.fg; el.style.background = c.bg; el.style.borderColor = c.border;
    el.title = 'Projeto: ' + (projectId || '—');
  }
  function projectBadge(projectId) { const b = h('span'); applyBadge(b, projectId); return b; }

  function stateChip(run, extraCls) {
    const v = runStateVisual(run.state);
    return h('span', { class: 'state-chip tone-' + v.tone + (extraCls ? ' ' + extraCls : '') }, icon(v.icon), v.text);
  }

  function routeShort(route) { const s = String(route || ''); const i = s.indexOf(':'); return i >= 0 ? s.slice(i + 1) : s; }

  /** Reconcilia filhos de `container` por chave, reaproveitando nós. */
  function reconcile(container, items, keyOf, create, update) {
    const map = container._nodes || (container._nodes = new Map());
    const seen = new Set();
    items.forEach(function (it) { seen.add(keyOf(it)); });
    map.forEach(function (node, k) { if (!seen.has(k)) { node.remove(); map.delete(k); } });
    items.forEach(function (it, idx) {
      const k = keyOf(it);
      let node = map.get(k);
      const isNew = !node;
      if (isNew) { node = create(it); map.set(k, node); }
      update(node, it, isNew);
      if (container.children[idx] !== node) container.insertBefore(node, container.children[idx] || null);
    });
  }

  /** Nó cujo conteúdo só é reconstruído quando a assinatura muda. */
  function sigNode(tag, cls, attrs) {
    return function () { return h(tag, Object.assign({ class: cls }, attrs || {})); };
  }
  function sigUpdate(sigFn, buildFn, decorate) {
    return function (node, item) {
      if (decorate) decorate(node, item);
      const sig = sigFn(item);
      if (node._sig !== sig) { node._sig = sig; node.replaceChildren.apply(node, [].concat(buildFn(item)).filter(function (n) { return n != null && n !== false; })); }
    };
  }

  function lsGet() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (e) { return {}; } }
  function lsSet(patch) { try { localStorage.setItem(STORE_KEY, JSON.stringify(Object.assign(lsGet(), patch))); } catch (e) { /* ignora */ } }

  /* =====================================================================
   * 3. Estado
   * ===================================================================== */

  const state = {
    snapshot: null,
    receivedAt: 0,
    conn: 'connecting',
    filters: { project: 'all', q: '', showDone: true },
    doneLimit: DONE_PAGE,
    doneCollapsed: false,
    drawer: { open: false, runId: null, stage: null, sig: '', trigger: null },
    prevStates: null,
    notify: false,
    view: null,
  };
  const conn = { es: null, failures: 0, pollTimer: null, polling: false, inflight: false, retryTimer: null, abort: null };

  function genMs() {
    const g = state.snapshot ? parseTime(state.snapshot.generated_at) : NaN;
    return isFinite(g) ? g : (state.receivedAt || NaN);
  }

  /* =====================================================================
   * 4. Renderização
   * ===================================================================== */

  const el = {
    board: $('line-live-board'), app: $('live-app'), chips: $('project-chips'), search: $('search-input'),
    toggleDone: $('toggle-done'), notifyBtn: $('notify-btn'), connPill: $('conn-pill'), connText: $('conn-text'),
    fresh: $('freshness'), banner: $('alert-banner'), attnLive: $('attention-live'),
    secAttention: $('sec-attention'), attnList: $('attention-list'), attnCount: $('attention-count'),
    lanes: $('lanes'), lanesEmpty: $('lanes-empty'), runningCount: $('running-count'),
    secDone: $('sec-done'), doneToggle: $('done-toggle'), doneList: $('done-list'), doneEmpty: $('done-empty'),
    doneCount: $('done-count'), doneMore: $('done-more'),
    events: $('events'), eventsEmpty: $('events-empty'), backlog: $('backlog'), backlogEmpty: $('backlog-empty'),
    secOff: $('sec-offline'), offList: $('off-list'), offCount: $('off-count'), foot: $('foot'),
    drawer: $('drawer'), drawerPanel: document.querySelector('.drawer-panel'), drawerContent: $('drawer-content'),
    drawerClose: $('drawer-close'),
  };

  function render() {
    const snap = state.snapshot;
    if (!snap) { renderConn(Date.now()); return; }
    const view = state.view = deriveView(snap, state.filters);
    const steps = [renderChips, renderBanner, renderKpis, renderAttention, renderLanes, renderDone, renderEvents, renderBacklog, renderOffLine, renderFoot, renderTitle, renderDrawer];
    steps.forEach(function (fn) {
      try { fn(view, snap); } catch (e) { if (root.console) console.error('[LineLive] falha em ' + fn.name, e); }
    });
    el.board.classList.remove('is-loading');
    renderConn(Date.now());
    tick();
  }

  /* ---- Barra superior ---- */

  function renderChips(view) {
    const items = [{ id: 'all', label: 'Todos', count: view.totalCount }].concat(view.projects.map(function (p) { return { id: p.id, label: p.id, count: p.count }; }));
    const cur = state.filters.project;
    if (cur && cur !== 'all' && !view.projects.some(function (p) { return p.id === cur; })) items.push({ id: cur, label: cur, count: 0 });
    reconcile(el.chips, items, function (i) { return i.id; },
      function () {
        return h('button', { class: 'chip', type: 'button' }, h('span', { class: 'chip-dot' }), h('span', { class: 'chip-l' }), h('span', { class: 'chip-n' }));
      },
      function (node, i) {
        node.dataset.project = i.id;
        node.setAttribute('aria-pressed', String(cur === i.id || (i.id === 'all' && (!cur || cur === 'all'))));
        const dot = node.children[0];
        if (i.id === 'all') dot.style.display = 'none'; else { dot.style.display = ''; dot.style.background = projectColor(i.id).fg; }
        setText(node.children[1], i.label);
        setText(node.children[2], String(i.count));
      });
  }

  function renderConn(now) {
    let c = state.conn, text;
    const gen = genMs();
    const stale = c === 'live' && isFinite(gen) && now - gen > STALE_LIVE_MS;
    if (stale) { c = 'stale'; text = 'Dados desatualizados'; }
    else text = { live: 'Ao vivo', reconnecting: 'Reconectando…', connecting: 'Conectando…', polling: 'Atualização periódica', offline: 'Offline' }[c] || c;
    setClass(el.connPill, 'conn conn-' + c + (stale && now - gen > STALE_LIVE_MS * 2 ? ' is-bad' : ''));
    setText(el.connText, text);
    const title = {
      live: 'Conectado ao stream em tempo real.', reconnecting: 'Reconectando ao stream…', connecting: 'Conectando…',
      polling: 'Stream indisponível — consultando a API a cada 10 s.', offline: 'Sem conexão com o servidor. Exibindo os últimos dados recebidos.',
      stale: 'Conectado, mas o servidor não enviou dados novos recentemente.',
    }[c];
    if (title) el.connPill.title = title;

    let ft, fc = 'fresh';
    if (!isFinite(gen) || !state.snapshot) { ft = 'aguardando dados…'; fc += ' fresh-none'; }
    else {
      const age = (now - gen) / 1000;
      ft = 'dados de ' + formatClockTime(gen) + ' · ' + formatAge(gen, now);
      if (age > 60) fc += ' fresh-bad'; else if (age > 20) fc += ' fresh-warn';
    }
    setClass(el.fresh, fc);
    setText(el.fresh, ft);
  }

  function renderBanner(view, snap) {
    const lines = [];
    let bad = false;
    const gen = genMs();
    const when = isFinite(gen) ? formatClockTime(gen) : '—';
    const src = snap.source || {};
    if (src.status === 'error') { lines.push(['Fonte indisponível', ' — exibindo dados de ' + when]); bad = true; }
    else if (src.status === 'missing') { lines.push(['Fonte de dados não encontrada', ' — exibindo dados de ' + when]); bad = true; }
    if (state.conn === 'offline') { lines.push(['Sem conexão com o servidor', ' — exibindo dados de ' + when]); bad = true; }
    (Array.isArray(snap.warnings) ? snap.warnings : []).forEach(function (w) { lines.push(['Aviso: ', String(w)]); });
    if (!lines.length) { el.banner.hidden = true; return; }
    const sig = lines.map(function (l) { return l.join(''); }).join('\n') + bad;
    if (el.banner._sig !== sig) {
      el.banner._sig = sig;
      el.banner.replaceChildren(h('div', { class: 'alert-inner' }, lines.map(function (l) { return h('div', null, h('strong', { text: l[0] }), l[1]); })));
    }
    setClass(el.banner, 'alert-banner' + (bad ? ' is-bad' : ''));
    el.banner.hidden = false;
  }

  /* ---- KPIs ---- */

  function kpiSet(id, value, sub, opts) {
    const k = $(id);
    setText(k.querySelector('[data-k="value"]'), value);
    const s = k.querySelector('[data-k="sub"]');
    setText(s, sub || ' ');
    s.classList.toggle('is-bad', !!(opts && opts.bad));
    return k;
  }

  function renderKpis(view, snap) {
    const k = snap.kpis || {};
    kpiSet('kpi-active', String(k.active_runs != null ? k.active_runs : '–'), (k.running_stages || 0) + ' etapa' + (k.running_stages === 1 ? '' : 's') + ' rodando');
    const att = $('kpi-attention');
    kpiSet('kpi-attention', String(k.attention != null ? k.attention : '–'), k.attention ? 'aguardando ação' : 'nada pendente');
    att.classList.toggle('is-hot', (k.attention || 0) > 0);
    kpiSet('kpi-done', String(k.completed_24h != null ? k.completed_24h : '–'), (k.failed_24h || 0) + ' falha' + (k.failed_24h === 1 ? '' : 's'), { bad: (k.failed_24h || 0) > 0 });
    kpiSet('kpi-cycle', k.median_cycle_seconds_7d != null ? formatDuration(k.median_cycle_seconds_7d) : '—', 'p85 ' + (k.p85_cycle_seconds_7d != null ? formatDuration(k.p85_cycle_seconds_7d) : '—'));
    kpiSet('kpi-cost', formatMoney(k.cost_24h_usd), 'últimas 24 h');
    const impl = (snap.backlog || []).reduce(function (a, b) { return a + (b.implementing || 0); }, 0);
    kpiSet('kpi-backlog', String(k.planned_backlog != null ? k.planned_backlog : '–'), impl + ' em implementação');
    renderSpark($('kpi-done').querySelector('[data-k="spark"]'), k.throughput_7d);
  }

  function renderSpark(host, series) {
    const data = Array.isArray(series) ? series : [];
    const sig = JSON.stringify(data);
    if (host._sig === sig) return;
    host._sig = sig;
    host.replaceChildren();
    if (!data.length) return;
    const NS = 'http://www.w3.org/2000/svg';
    const W = 6, G = 3, H = 26;
    const max = Math.max.apply(null, data.map(function (d) { return d.completed || 0; }).concat([1]));
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', String(data.length * (W + G) - G));
    svg.setAttribute('height', String(H));
    svg.setAttribute('viewBox', '0 0 ' + (data.length * (W + G) - G) + ' ' + H);
    data.forEach(function (d, i) {
      const hh = Math.max(2, Math.round(((d.completed || 0) / max) * H));
      const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', String(i * (W + G))); r.setAttribute('y', String(H - hh));
      r.setAttribute('width', String(W)); r.setAttribute('height', String(hh)); r.setAttribute('rx', '1.5');
      const t = document.createElementNS(NS, 'title');
      t.textContent = d.date + ': ' + (d.completed || 0) + ' concluído(s)';
      r.appendChild(t);
      svg.appendChild(r);
    });
    host.appendChild(svg);
    host.setAttribute('title', 'Concluídos por dia (7 d): ' + data.map(function (d) { return d.completed || 0; }).join(', '));
  }

  /* ---- Precisa de você ---- */

  function attentionInfo(run) {
    const a = run.attention || {};
    const by = stageMap(run);
    const cur = by[a.stage || run.current_stage] || {};
    return {
      stage: a.stage || run.current_stage,
      cause: a.cause_code || cur.cause_code || null,
      diag: a.diagnostic || cur.diagnostic || '',
      since: parseTime(a.since || run.updated_at),
    };
  }

  function attentionBuild(item, snap) {
    const run = item.run;
    const stalled = item.kind === 'stalled';
    const info = attentionInfo(run);
    const kids = [];
    kids.push(h('div', { class: 'att-top' },
      h('span', { class: 'ticket-id', text: run.ticket_id }), projectBadge(run.project_id),
      stalled
        ? h('span', { class: 'state-chip tone-stalled' }, icon('alert'), 'Possivelmente travado')
        : h('span', { class: 'state-chip tone-attention' }, icon('hand'), 'Precisa de você')));
    kids.push(h('div', { class: 'att-title', text: run.title || '(sem título)' }));
    const stageText = info.stage ? stageLabel(snap, info.stage, stageMap(run)[info.stage]) : '—';
    kids.push(h('div', { class: 'att-meta' }, h('span', null, 'Etapa: ', h('strong', { text: stageText })), '·', liveEl('span', 'age', info.since)));
    const diag = stalled ? (run.stalled_reason || info.diag) : info.diag;
    if (diag) kids.push(h('div', { class: 'att-diag', text: diag, title: diag }));
    if (!stalled) {
      if (info.cause === 'grill_pending') {
        kids.push(h('div', { class: 'att-hint' }, 'Responder o Grill no Telegram ou no DarkHub · ', h('a', { href: '/#demands', text: 'abrir demandas' })));
      } else if (info.cause) {
        kids.push(h('div', { class: 'att-meta' }, 'Causa: ', h('span', { class: 'cause', text: info.cause })));
      }
    }
    return kids;
  }

  function renderAttention(view, snap) {
    const items = view.attention;
    el.secAttention.hidden = items.length === 0;
    setText(el.attnCount, String(items.length));
    const msg = view.attentionAll === 0 ? 'Nada precisa de você agora.' : view.attentionAll + (view.attentionAll === 1 ? ' item precisa' : ' itens precisam') + ' de você.';
    setText(el.attnLive, msg);
    reconcile(el.attnList, items, function (i) { return i.run.run_id + ':' + i.kind; },
      sigNode('div', 'att-card', { tabindex: '0' }),
      sigUpdate(
        function (i) { return JSON.stringify([i.kind, i.run.title, i.run.attention, i.run.stalled_reason, i.run.current_stage, i.run.updated_at, stageMap(i.run)[i.run.current_stage] && stageMap(i.run)[i.run.current_stage].diagnostic]); },
        function (i) { return attentionBuild(i, snap); },
        function (node, i) {
          node.dataset.openRun = i.run.run_id;
          node.classList.toggle('is-stalled', i.kind === 'stalled');
          node.setAttribute('aria-label', i.run.ticket_id + ': ' + (i.kind === 'stalled' ? 'possivelmente travado' : 'precisa de você'));
        }));
  }

  /* ---- Raias (Em andamento) ---- */

  function createStep() {
    const node = h('div', { class: 'node' });
    const badge = h('span', { class: 'node-badge', hidden: true });
    node.append(badge);
    const label = h('div', { class: 'step-label' });
    const sub = h('div', { class: 'step-sub' });
    const li = h('li', { class: 'step st-not_reached' }, node, label, sub);
    li._r = { node: node, badge: badge, label: label, sub: sub, icon: null };
    return li;
  }

  function updateStep(li, it, isNew) {
    const r = li._r;
    const st = it.stage;
    const status = st ? st.status : 'not_reached';
    const v = stageVisual(status);
    li.dataset.openRun = it.run.run_id;
    li.dataset.stage = it.key;
    if (li._status !== status) {
      const changed = li._status !== undefined;
      li._status = status;
      li.className = 'step st-' + v.key;
      if (r.icon) r.icon.remove();
      r.icon = v.icon ? icon(v.icon) : null;
      if (r.icon) r.node.insertBefore(r.icon, r.badge);
      if (changed) {
        li.classList.add('is-changed');
        setTimeout(function () { li.classList.remove('is-changed'); }, 1200);
      }
    }
    li.classList.toggle('is-filled', status === 'succeeded');
    setText(r.label, SHORT_LABELS[it.key] || it.label);
    const n = st ? (st.retry_count > 0 ? st.retry_count : (status === 'retry' || status === 'replan') && st.attempts > 1 ? st.attempts - 1 : 0) : 0;
    r.badge.hidden = n <= 0;
    setText(r.badge, '×' + n);
    li.title = it.label + ' — ' + v.text + (n > 0 ? ' (' + n + ' nova(s) tentativa(s))' : '') + (st && st.cause_code ? ' · ' + st.cause_code : '');
    li.setAttribute('aria-label', li.title);
    // sub: cronômetro vivo, duração ou texto curto de estado
    const started = st ? parseTime(st.started_at) : NaN;
    if (status === 'running' && isFinite(started)) {
      r.sub.dataset.live = 'timer'; r.sub.dataset.ts = String(started);
      liveRefresh(r.sub, Date.now());
    } else {
      delete r.sub.dataset.live; delete r.sub.dataset.ts;
      setText(r.sub, status === 'succeeded' ? formatDuration(st && st.duration_seconds) : (status === 'running' ? '…' : v.sub));
    }
  }

  function createLane() {
    const r = {
      id: h('span', { class: 'ticket-id' }), badge: h('span', { class: 'proj-badge' }), chip: h('span'),
      stalled: h('span', { class: 'state-chip tone-stalled', hidden: true }, icon('alert'), 'Possivelmente travado'),
      title: h('div', { class: 'lane-title' }),
      age: h('span', { class: 'age', 'data-live': 'elapsed' }), cost: h('span', { class: 'cost' }),
      stepper: h('ol', { class: 'stepper', 'aria-label': 'Etapas do run' }), timebar: h('div', { class: 'timebar' }), now: h('div', { class: 'lane-now' }),
    };
    const lane = h('article', { class: 'lane', tabindex: '0' },
      h('div', { class: 'lane-head' },
        h('div', { class: 'lane-id-row' }, r.id, r.badge, r.chip, r.stalled),
        r.title,
        h('div', { class: 'lane-meta' }, r.age, r.cost)),
      h('div', { class: 'lane-body' }, h('div', { class: 'stepper-wrap' }, r.stepper), r.timebar, r.now));
    lane._r = r;
    return lane;
  }

  function laneNowNodes(run, snap) {
    if (run.state === 'queued') return [h('span', { text: 'Na fila — aguardando um worker.' })];
    const by = stageMap(run);
    const key = run.current_stage;
    const st = key ? by[key] : null;
    if (!key || !st) return [h('span', { text: 'Preparando a próxima etapa…' })];
    const v = stageVisual(st.status);
    const out = [h('strong', { text: stageLabel(snap, key, st) }), h('span', { text: '· ' + v.text.toLowerCase() })];
    const started = parseTime(st.started_at);
    if (st.status === 'running' && isFinite(started)) out.push(liveEl('span', 'timer', started, 't-run'));
    const parts = [];
    if (st.worker) parts.push(st.worker);
    if (st.route) parts.push(routeShort(st.route));
    if (parts.length) out.push(h('span', { class: 'mono', text: '· ' + parts.join(' · '), title: (st.route || '') }));
    if (st.attempts > 1) out.push(h('span', { text: '· tentativa ' + st.attempts }));
    if (run.state === 'attention') {
      const info = attentionInfo(run);
      if (info.cause) out.push(h('span', { class: 'cause', text: info.cause }));
    }
    return out;
  }

  function updateLane(lane, run, snap) {
    const r = lane._r;
    lane.dataset.openRun = run.run_id;
    const rs = runStateVisual(run.state);
    setClass(lane, 'lane state-' + run.state + (run.stalled ? ' is-stalled' : ''));
    setText(r.id, run.ticket_id);
    applyBadge(r.badge, run.project_id);
    setText(r.title, run.title || '(sem título)');
    r.title.title = run.title || '';
    const chipCls = 'state-chip tone-' + rs.tone;
    if (r.chip.className !== chipCls || r.chip._st !== run.state) {
      r.chip.className = chipCls; r.chip._st = run.state;
      r.chip.replaceChildren(icon(rs.icon), rs.text);
    }
    r.stalled.hidden = !run.stalled;
    r.stalled.title = run.stalled_reason || 'Sem progresso além do esperado';
    lane.setAttribute('aria-label', run.ticket_id + ' — ' + (run.title || '') + ' — ' + rs.text);

    const gen = parseTime(snap.generated_at);
    let startTs = parseTime(run.created_at);
    if (run.age_seconds != null && isFinite(gen)) startTs = gen - run.age_seconds * 1000;
    r.age.dataset.prefix = run.state === 'queued' ? 'na fila há ' : 'em andamento há ';
    r.age.dataset.ts = String(startTs);
    liveRefresh(r.age, Date.now());
    setText(r.cost, formatMoney(run.total_cost_usd) + (run.iterations_total > 1 ? ' · ' + run.iterations_total + ' iterações' : ''));

    // stepper
    const by = stageMap(run);
    const steps = stageKeys(snap, run).map(function (k) { return { run: run, key: k, stage: by[k] || null, label: stageLabel(snap, k, by[k]) }; });
    reconcile(r.stepper, steps, function (s) { return s.key; }, createStep, updateStep);

    // barra de distribuição de tempo
    const now = Date.now();
    const segs = [];
    steps.forEach(function (s) {
      if (!s.stage) return;
      let d = Number(s.stage.duration_seconds) || 0;
      if (s.stage.status === 'running') { const st0 = parseTime(s.stage.started_at); if (isFinite(st0)) d = Math.max(d, (now - st0) / 1000); }
      if (d > 0) segs.push({ key: s.key, dur: d, status: s.stage.status, label: s.label });
    });
    r.timebar.classList.toggle('is-empty', segs.length === 0);
    reconcile(r.timebar, segs, function (s) { return s.key; }, function () { return h('span', { class: 'tseg' }); },
      function (node, s) {
        node.className = 'tseg tone-' + stageVisual(s.status).tone;
        node.style.flexGrow = String(Math.max(s.dur, 1));
        node.title = s.label + ': ' + formatDuration(s.dur) + ' (' + stageVisual(s.status).text.toLowerCase() + ')';
      });
    r.timebar.setAttribute('aria-label', 'Distribuição de tempo por etapa: ' + segs.map(function (s) { return s.label + ' ' + formatDuration(s.dur); }).join(', '));

    // linha da etapa atual
    const stc = by[run.current_stage] || {};
    const sig = JSON.stringify([run.state, run.current_stage, stc.status, stc.worker, stc.route, stc.attempts, stc.started_at, run.attention && run.attention.cause_code]);
    if (r.now._sig !== sig) { r.now._sig = sig; r.now.replaceChildren.apply(r.now, laneNowNodes(run, snap)); }
  }

  function renderLanes(view, snap) {
    el.lanes.querySelectorAll('.skeleton-lane').forEach(function (n) { n.remove(); });
    setText(el.runningCount, String(view.active.length));
    reconcile(el.lanes, view.active, function (r) { return r.run_id; }, createLane, function (lane, run) { updateLane(lane, run, snap); });

    if (view.active.length) { el.lanesEmpty.hidden = true; return; }
    el.lanesEmpty.hidden = false;
    const kids = [];
    if (view.hiddenByFilter > 0 && view.filtersActive) {
      kids.push(h('strong', { text: 'Nenhum run corresponde aos filtros.' }));
      kids.push(h('button', { class: 'btn', type: 'button', 'data-clear-filters': '' }, 'Limpar filtros'));
    } else {
      kids.push(h('strong', { text: 'Nenhum run ativo — a fábrica está ociosa.' }));
      let next = null;
      (snap.backlog || []).forEach(function (b) { if (!next && b.next && b.next.length && (state.filters.project === 'all' || b.project_id === state.filters.project)) next = b.next[0]; });
      if (next) kids.push(h('div', { class: 'next' }, 'Próximo do backlog: ', h('span', { class: 'ticket-id', text: next.id }), ' — ' + (next.title || '')));
    }
    const sig = JSON.stringify(kids.map(function (k) { return k.textContent; }));
    if (el.lanesEmpty._sig !== sig) { el.lanesEmpty._sig = sig; el.lanesEmpty.replaceChildren.apply(el.lanesEmpty, kids); }
  }

  /* ---- Concluídos ---- */

  function doneBuild(run, snap) {
    const rs = runStateVisual(run.state);
    const by = stageMap(run);
    const dots = h('span', { class: 'dots', 'aria-hidden': 'true' }, stageKeys(snap, run).slice(0, 8).map(function (k) {
      const st = by[k];
      return h('i', { class: 'dot-s tone-' + stageVisual(st ? st.status : 'not_reached').tone, title: stageLabel(snap, k, st) + ': ' + stageVisual(st ? st.status : 'not_reached').text });
    }));
    return [
      h('span', { class: 'state-chip tone-' + rs.tone }, icon(rs.icon), rs.text),
      h('span', { class: 'ticket-id', text: run.ticket_id }),
      h('span', { class: 'done-title', text: run.title || '(sem título)', title: run.title || '' }),
      h('span', { class: 'done-extra' },
        h('span', { text: 'ciclo ' + formatDuration(run.cycle_seconds), title: 'Cycle time' }),
        h('span', { text: formatMoney(run.total_cost_usd) }),
        liveEl('span', 'age', parseTime(run.completed_at || run.updated_at)),
        dots),
    ];
  }

  function renderDone(view, snap) {
    const show = state.filters.showDone;
    el.secDone.hidden = !show;
    if (!show) return;
    setText(el.doneCount, String(view.done.length));
    el.doneToggle.setAttribute('aria-expanded', String(!state.doneCollapsed));
    const body = !state.doneCollapsed;
    el.doneList.hidden = !body;
    const items = view.done.slice(0, state.doneLimit);
    el.doneEmpty.hidden = !(body && view.done.length === 0);
    el.doneMore.hidden = !(body && view.done.length > state.doneLimit);
    setText(el.doneMore, 'Ver mais (' + (view.done.length - state.doneLimit) + ')');
    reconcile(el.doneList, items, function (r) { return r.run_id; },
      sigNode('div', 'done-row', { tabindex: '0' }),
      sigUpdate(function (r) { return JSON.stringify([r.state, r.title, r.cycle_seconds, r.total_cost_usd, r.completed_at, r.stages && r.stages.map(function (s) { return s.status; })]); },
        function (r) { return doneBuild(r, snap); },
        function (node, r) { node.dataset.openRun = r.run_id; node.setAttribute('aria-label', r.ticket_id + ' ' + runStateVisual(r.state).text); }));
  }

  /* ---- Lateral ---- */

  function renderEvents(view, snap) {
    const items = view.events.slice(0, 30);
    el.eventsEmpty.hidden = items.length > 0;
    reconcile(el.events, items, function (e) { return [e.at, e.run_id, e.kind, e.stage].join('|'); },
      function () { return h('li', { class: 'ev-wrap' }); },
      sigUpdate(function (e) { return e.message + '|' + e.title; },
        function (e) {
          const k = EVENT_KINDS[e.kind] || { icon: 'play', tone: 'info', label: e.kind };
          const ts = parseTime(e.at);
          return h('button', { class: 'ev', type: 'button', 'data-open-run': e.run_id, 'data-stage': e.stage || null, title: k.label + (e.title ? ' — ' + e.title : '') },
            h('span', { class: 'ev-ico tone-' + k.tone }, icon(k.icon)),
            h('span', { class: 'ev-body' },
              h('span', { class: 'ev-top' },
                h('span', { class: 'ticket-id', text: e.ticket_id }),
                liveEl('span', 'age', ts, 'ev-time')),
              h('span', { class: 'ev-msg', text: (e.stage ? stageLabel(snap, e.stage) + ' · ' : '') + (e.message || k.label) })));
        }));
  }

  function renderBacklog(view) {
    el.backlogEmpty.hidden = view.backlog.length > 0;
    reconcile(el.backlog, view.backlog, function (b) { return b.project_id; }, sigNode('div', 'bl'),
      sigUpdate(function (b) { return JSON.stringify(b); },
        function (b) {
          const tot = Math.max(b.total || (b.planned + b.implementing + b.completed), 1);
          const pct = function (n) { return Math.max(0, Math.min(100, (n / tot) * 100)); };
          const seg = function (cls, n) { return n > 0 ? h('i', { class: cls, style: 'width:' + pct(n) + '%' }) : null; };
          return [
            h('div', { class: 'bl-head' }, projectBadge(b.project_id), h('span', { class: 'mono', style: 'font-size:11.5px;color:var(--muted)', text: (b.total || 0) + ' tickets' })),
            h('div', { class: 'stack', role: 'img', 'aria-label': b.planned + ' planejados, ' + b.implementing + ' em implementação, ' + b.completed + ' concluídos' },
              seg('s-done', b.completed), seg('s-impl', b.implementing), seg('s-plan', b.planned)),
            h('div', { class: 'bl-legend' },
              h('span', { class: 'lg lg-plan' }, h('b', { text: String(b.planned) }), ' planejados'),
              h('span', { class: 'lg lg-impl' }, h('b', { text: String(b.implementing) }), ' em implementação'),
              h('span', { class: 'lg lg-done' }, h('b', { text: String(b.completed) }), ' concluídos')),
            (b.next && b.next.length)
              ? h('div', { class: 'bl-next' }, b.next.slice(0, 5).map(function (n) {
                return h('div', { class: 'bl-item' }, h('span', { class: 'ticket-id', text: n.id }), h('span', { class: 'bl-title', text: n.title || '', title: n.title || '' }), n.horizon ? h('span', { class: 'hz', text: n.horizon }) : null);
              }))
              : null,
          ];
        }));
  }

  function renderOffLine(view) {
    el.secOff.hidden = view.offLine.length === 0;
    setText(el.offCount, String(view.offLine.length));
    reconcile(el.offList, view.offLine, function (t) { return t.id; }, sigNode('li', 'off-item'),
      sigUpdate(function (t) { return JSON.stringify(t); },
        function (t) {
          return [
            h('div', { class: 'row' }, h('span', { class: 'ticket-id', text: t.id }), projectBadge(t.project_id), h('span', { class: 'state-chip tone-info', text: t.status || 'em desenvolvimento' })),
            h('div', { class: 'off-title', text: t.title || '' }),
            h('div', { class: 'off-age' }, 'atualizado ', liveEl('span', 'age', parseTime(t.updated_at))),
          ];
        }));
  }

  function renderFoot(view, snap) {
    const src = snap.source || {};
    const t = 'Fonte: ' + (src.backend || '—') + ' · ' + (src.status || '—') + ' · versão ' + (snap.version != null ? snap.version : '—');
    setText(el.foot, t);
  }

  function renderTitle(view) {
    const n = view.attentionAll;
    const t = (n > 0 ? '(' + n + ') ' : '') + 'Esteira ao vivo';
    if (document.title !== t) document.title = t;
  }

  /* =====================================================================
   * 5. Gaveta de detalhes
   * ===================================================================== */

  function attemptsOf(st, run) {
    if (st.history && st.history.length) return st.history;
    if (st.started_at) return [{ iteration: 1, status: st.status, started_at: st.started_at, finished_at: st.finished_at, duration_seconds: st.duration_seconds, cost_usd: st.cost_usd, cause_code: st.cause_code, synthetic: true }];
    return [];
  }

  function copyBtn(value, label, fk) {
    return h('button', { class: 'btn btn-xs', type: 'button', 'data-copy': value, 'data-fk': fk || null, 'aria-label': 'Copiar ' + label }, 'Copiar');
  }

  function buildGantt(run, snap) {
    const rows = [];
    const bars = [];
    let t0 = parseTime(run.created_at), tMax = -Infinity;
    stageKeys(snap, run).forEach(function (k) {
      const st = stageMap(run)[k];
      if (!st) return;
      const at = attemptsOf(st, run).map(function (a) { return { a: a, s: parseTime(a.started_at), e: parseTime(a.finished_at) }; }).filter(function (x) { return isFinite(x.s); });
      if (!at.length) return;
      at.forEach(function (x) { if (!isFinite(t0) || x.s < t0) t0 = x.s; tMax = Math.max(tMax, isFinite(x.e) ? x.e : x.s); });
      rows.push({ k: k, st: st, at: at });
    });
    if (!rows.length) return h('div', { class: 'tl-dim', text: 'Nenhuma etapa iniciada ainda.' });
    const active = run.completed_at == null && ACTIVE_RANK[run.state] !== undefined;
    const t1 = active ? null : (isFinite(parseTime(run.completed_at)) ? parseTime(run.completed_at) : tMax);
    const g = h('div', { class: 'gantt', 'data-t0': String(t0), 'data-t1': t1 == null ? null : String(t1), role: 'img', 'aria-label': 'Linha do tempo das tentativas por etapa' });
    rows.forEach(function (row) {
      const track = h('div', { class: 'g-track' });
      row.at.forEach(function (x) {
        const v = stageVisual(x.a.status);
        track.append(h('i', { class: 'g-bar tone-' + v.tone, 'data-s': String(x.s), 'data-e': isFinite(x.e) ? String(x.e) : null,
          title: stageLabel(snap, row.k, row.st) + ' #' + (x.a.iteration != null ? x.a.iteration : 1) + ' · ' + v.text + ' · ' + formatClockTime(x.s) + ' → ' + (isFinite(x.e) ? formatClockTime(x.e) : 'em curso') }));
      });
      g.append(h('div', { class: 'g-row' }, h('span', { class: 'g-label', text: SHORT_LABELS[row.k] || stageLabel(snap, row.k, row.st) }), track));
    });
    g.append(h('div', { class: 'g-axis' }, h('span', { text: formatClockTime(t0) }), h('span', { class: 'g-end', text: t1 == null ? 'agora' : formatClockTime(t1) })));
    positionGantt(g, Date.now());
    return g;
  }

  function positionGantt(g, now) {
    const t0 = Number(g.dataset.t0);
    const t1 = g.dataset.t1 ? Number(g.dataset.t1) : now;
    const span = Math.max(t1 - t0, 1000);
    g.querySelectorAll('.g-bar').forEach(function (b) {
      const s = Number(b.dataset.s), e = b.dataset.e ? Number(b.dataset.e) : now;
      const left = Math.max(0, Math.min(100, ((s - t0) / span) * 100));
      const width = Math.max(0.6, Math.min(100 - left, ((e - s) / span) * 100));
      b.style.left = left.toFixed(2) + '%'; b.style.width = width.toFixed(2) + '%';
    });
  }

  function evidenceNodes(refs) {
    if (!refs || !refs.length) return null;
    return h('div', { class: 'evidence' }, h('div', { class: 'ev-title', text: 'Evidências' }), refs.map(function (ref, i) {
      const s = String(ref);
      let link = false;
      try { const u = new URL(s); link = u.protocol === 'http:' || u.protocol === 'https:'; } catch (e) { link = false; }
      if (link) return h('div', { class: 'ev-ref' }, h('a', { href: s, target: '_blank', rel: 'noopener noreferrer', text: s }));
      return h('div', { class: 'ev-ref' }, h('span', { class: 'mono', text: s }), copyBtn(s, 'evidência', 'ev-' + i));
    }));
  }

  function buildStageBlock(run, snap, key, st) {
    const v = stageVisual(st ? st.status : 'not_reached');
    const label = stageLabel(snap, key, st);
    const li = h('li', { class: 'tl-stage st-' + v.key, 'data-stage-block': key });
    li.append(h('div', { class: 'tl-dot' }, v.icon ? icon(v.icon) : null));
    const main = h('div', { class: 'tl-main' });
    li.append(main);
    const head = h('div', { class: 'tl-head' }, h('strong', { text: label }), h('span', { class: 'state-chip tone-' + v.tone }, v.icon ? icon(v.icon) : null, v.text));
    main.append(head);
    if (!st || st.status === 'not_reached') { main.append(h('div', { class: 'tl-dim', text: 'Esta etapa ainda não foi alcançada.' })); return li; }
    const sum = [];
    if (st.attempts) sum.push(st.attempts + (st.attempts === 1 ? ' tentativa' : ' tentativas'));
    if (st.max_retries != null) sum.push('retries ' + (st.retry_count || 0) + '/' + st.max_retries);
    if (sum.length) head.append(h('span', { class: 'tl-sum', text: sum.join(' · ') }));

    const kv = [];
    const add = function (k, vv) { if (vv != null && vv !== '') kv.push(h('div', null, h('dt', { text: k }), h('dd', null, vv))); };
    const started = parseTime(st.started_at);
    if (st.status === 'running' && isFinite(started)) add('Em execução', liveEl('span', 'timer', started));
    else if (st.duration_seconds != null) add('Duração', formatDuration(st.duration_seconds));
    if (isFinite(started)) add('Período', formatClockTime(started) + ' → ' + (isFinite(parseTime(st.finished_at)) ? formatClockTime(parseTime(st.finished_at)) : 'em curso'));
    if (st.cost_usd != null) add('Custo', formatMoney(st.cost_usd));
    if (st.role) add('Papel', st.role);
    if (st.worker) add('Worker', st.worker);
    if (st.route) add('Rota', st.route);
    const lease = parseTime(st.lease_expires_at);
    if (isFinite(lease)) add('Lease expira', liveEl('span', 'until', lease));
    if (st.timeout_seconds != null) add('Timeout', formatDuration(st.timeout_seconds));
    if (st.cause_code) add('Causa', st.cause_code);
    if (kv.length) main.append(h('dl', { class: 'tl-kv' }, kv));
    if (st.diagnostic) main.append(h('pre', { class: 'diag', text: st.diagnostic }));

    const attempts = st.history && st.history.length ? st.history : [];
    if (attempts.length) {
      main.append(h('div', { class: 'attempts' }, attempts.map(function (a) {
        const av = stageVisual(a.status);
        const s = parseTime(a.started_at), e = parseTime(a.finished_at);
        return h('div', { class: 'attempt' },
          h('span', { class: 'mono', text: '#' + (a.iteration != null ? a.iteration : '?') }),
          h('span', { class: 'state-chip tone-' + av.tone }, av.icon ? icon(av.icon) : null, av.text),
          h('span', { class: 'mono', text: (isFinite(s) ? formatClockTime(s) : '—') + ' → ' + (isFinite(e) ? formatClockTime(e) : 'em curso') }),
          a.duration_seconds != null ? h('span', { text: formatDuration(a.duration_seconds) }) : (av.key === 'running' && isFinite(s) ? liveEl('span', 'timer', s) : null),
          a.cost_usd != null ? h('span', { text: formatMoney(a.cost_usd) }) : null,
          a.cause_code ? h('span', { class: 'cause', text: a.cause_code }) : null);
      })));
    }
    const ev = evidenceNodes(st.evidence_refs);
    if (ev) main.append(ev);
    return li;
  }

  function buildDrawer(run, snap) {
    const rs = runStateVisual(run.state);
    const out = [];
    const gen = parseTime(snap.generated_at);
    const activeRun = ACTIVE_RANK[run.state] !== undefined;
    let startTs = parseTime(run.created_at);
    if (run.age_seconds != null && isFinite(gen)) startTs = gen - run.age_seconds * 1000;

    out.push(h('div', { class: 'dr-head' },
      h('div', { class: 'dr-id-row' }, h('span', { class: 'ticket-id', text: run.ticket_id }), projectBadge(run.project_id), stateChip(run),
        run.stalled ? h('span', { class: 'state-chip tone-stalled', title: run.stalled_reason || '' }, icon('alert'), 'Possivelmente travado') : null),
      h('div', { class: 'dr-title', text: run.title || '(sem título)' }),
      h('div', { class: 'dr-runid' }, 'run_id', h('code', { text: run.run_id }), copyBtn(run.run_id, 'run_id', 'copy-run'))));

    if (run.state === 'attention' || run.stalled) {
      const info = attentionInfo(run);
      const diag = run.stalled && run.state !== 'attention' ? (run.stalled_reason || info.diag) : info.diag;
      out.push(h('div', null,
        h('div', { class: 'dr-section-title', text: run.state === 'attention' ? 'Precisa de você' : 'Possivelmente travado' }),
        diag ? h('pre', { class: 'diag', style: 'border-left-color:var(--human)', text: diag }) : null,
        info.cause === 'grill_pending' ? h('p', { class: 'att-hint', style: 'margin-top:8px' }, 'Responder o Grill no Telegram ou no DarkHub · ', h('a', { href: '/#demands', text: 'abrir demandas' })) : null));
    }

    const cell = function (k, v) { return h('div', { class: 'dr-cell' }, h('dt', { text: k }), h('dd', null, v)); };
    out.push(h('dl', { class: 'dr-grid' },
      cell(activeRun ? 'Idade' : 'Cycle time', activeRun ? liveEl('span', 'elapsed', startTs) : formatDuration(run.cycle_seconds)),
      cell('Custo total', formatMoney(run.total_cost_usd)),
      cell('Iterações', String(run.iterations_total != null ? run.iterations_total : '—')),
      cell('Modo', run.mode || '—'),
      cell('Criado', formatDateTime(parseTime(run.created_at))),
      cell('Atualizado', formatDateTime(parseTime(run.updated_at))),
      cell('Concluído', run.completed_at ? formatDateTime(parseTime(run.completed_at)) : '—'),
      cell('Status do run', run.run_status || rs.text)));

    out.push(h('div', null, h('div', { class: 'dr-section-title', text: 'Tempo por tentativa' }), buildGantt(run, snap)));

    const by = stageMap(run);
    out.push(h('div', null, h('div', { class: 'dr-section-title', text: 'Linha do tempo por etapa' }),
      h('ol', { class: 'timeline' }, stageKeys(snap, run).map(function (k) { return buildStageBlock(run, snap, k, by[k]); }))));
    return out;
  }

  function findRun(id) {
    const runs = (state.snapshot && state.snapshot.runs) || [];
    for (let i = 0; i < runs.length; i++) if (runs[i].run_id === id) return runs[i];
    return null;
  }

  function renderDrawer(view, snap) {
    const d = state.drawer;
    if (!d.open) return;
    const run = findRun(d.runId);
    const sig = run ? JSON.stringify([run, snap.stage_order]) : 'missing:' + d.runId;
    if (sig === d.sig) return;
    const first = d.sig === '';
    d.sig = sig;
    const body = el.drawerContent;
    const top = body.scrollTop;
    const ae = document.activeElement;
    const fk = ae && body.contains(ae) ? ae.getAttribute('data-fk') : null;
    if (!run) {
      body.replaceChildren(h('div', { class: 'empty' }, h('strong', { text: 'Run não encontrado' }), 'Este run saiu da janela exibida pela esteira.'));
      return;
    }
    body.replaceChildren.apply(body, buildDrawer(run, snap));
    if (first && d.stage) {
      const target = body.querySelector('[data-stage-block="' + String(d.stage).replace(/[^\w-]/g, '') + '"]');
      if (target) { target.classList.add('is-target'); target.scrollIntoView({ block: 'start' }); }
    } else {
      body.scrollTop = top;
      if (d.stage) { const t = body.querySelector('[data-stage-block="' + String(d.stage).replace(/[^\w-]/g, '') + '"]'); if (t) t.classList.add('is-target'); }
    }
    if (fk) { const n = body.querySelector('[data-fk="' + fk.replace(/[^\w-]/g, '') + '"]'); if (n) n.focus({ preventScroll: true }); }
    tick();
  }

  function openDrawer(runId, stage, trigger) {
    state.drawer = { open: true, runId: runId, stage: stage || null, sig: '', trigger: trigger || document.activeElement };
    el.drawer.hidden = false;
    el.app.inert = true;
    document.body.style.overflow = 'hidden';
    if (state.snapshot) renderDrawer(state.view, state.snapshot);
    el.drawerClose.focus({ preventScroll: true });
  }

  function closeDrawer() {
    if (!state.drawer.open) return;
    const trig = state.drawer.trigger;
    const runId = state.drawer.runId;
    state.drawer = { open: false, runId: null, stage: null, sig: '', trigger: null };
    el.drawer.hidden = true;
    el.app.inert = false;
    document.body.style.overflow = '';
    let target = trig && document.contains(trig) && trig.closest ? trig.closest('[tabindex], button, a[href]') : null;
    if (!target) target = document.querySelector('[data-open-run="' + String(runId).replace(/["\\]/g, '') + '"]');
    if (target && target.focus) target.focus({ preventScroll: true });
  }

  function trapTab(e) {
    const f = Array.from(el.drawerPanel.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter(function (n) { return n.offsetParent !== null; });
    if (!f.length) { e.preventDefault(); el.drawerPanel.focus(); return; }
    const first = f[0], last = f[f.length - 1], ae = document.activeElement;
    if (!el.drawerPanel.contains(ae)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && ae === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && ae === last) { e.preventDefault(); first.focus(); }
  }

  /* =====================================================================
   * 6. Tick de 1 s: só texto (cronômetros, idades, frescor, gantt)
   * ===================================================================== */

  function tick() {
    if (document.hidden) return;
    const now = Date.now();
    document.querySelectorAll('[data-live]').forEach(function (n) { liveRefresh(n, now); });
    renderConn(now);
    if (state.drawer.open) el.drawerContent.querySelectorAll('.gantt').forEach(function (g) { positionGantt(g, now); });
  }

  /* =====================================================================
   * 7. Filtros, persistência, URL
   * ===================================================================== */

  function loadFilters() {
    const saved = lsGet();
    const f = state.filters;
    if (typeof saved.project === 'string') f.project = saved.project;
    if (typeof saved.showDone === 'boolean') f.showDone = saved.showDone;
    state.notify = saved.notify === true;
    try {
      const qs = new URLSearchParams(location.search);
      if (qs.has('project')) f.project = qs.get('project') || 'all';
      if (qs.get('done') === '0') f.showDone = false;
      if (qs.get('done') === '1') f.showDone = true;
    } catch (e) { /* ignora */ }
    el.toggleDone.checked = f.showDone;
  }

  function persistFilters() {
    lsSet({ project: state.filters.project, showDone: state.filters.showDone, notify: state.notify });
    try {
      const url = new URL(location.href);
      if (state.filters.project && state.filters.project !== 'all') url.searchParams.set('project', state.filters.project); else url.searchParams.delete('project');
      if (!state.filters.showDone) url.searchParams.set('done', '0'); else url.searchParams.delete('done');
      history.replaceState(null, '', url.pathname + (url.search || '') + url.hash);
    } catch (e) { /* ignora */ }
  }

  function filtersChanged() { persistFilters(); render(); }

  /* =====================================================================
   * 8. Notificações do navegador (opt-in)
   * ===================================================================== */

  function renderNotifyBtn() {
    if (typeof Notification === 'undefined') { el.notifyBtn.hidden = true; return; }
    el.notifyBtn.hidden = false;
    const perm = Notification.permission;
    const on = state.notify && perm === 'granted';
    el.notifyBtn.setAttribute('aria-pressed', String(on));
    el.notifyBtn.disabled = perm === 'denied';
    setText(el.notifyBtn, perm === 'denied' ? 'Avisos bloqueados' : on ? 'Avisos ativos' : 'Avisar no navegador');
    el.notifyBtn.title = perm === 'denied' ? 'Permita notificações nas configurações do navegador para ativar.' : 'Notifica quando um run novo precisar de você ou falhar.';
  }

  function toggleNotify() {
    if (typeof Notification === 'undefined') return;
    if (state.notify && Notification.permission === 'granted') { state.notify = false; persistFilters(); renderNotifyBtn(); return; }
    const done = function (p) { state.notify = p === 'granted'; persistFilters(); renderNotifyBtn(); };
    try {
      const r = Notification.requestPermission(done);
      if (r && r.then) r.then(done);
    } catch (e) { renderNotifyBtn(); }
  }

  function checkNotifications(snap) {
    const runs = Array.isArray(snap.runs) ? snap.runs : [];
    const cur = new Map(runs.map(function (r) { return [r.run_id, r.state]; }));
    const prev = state.prevStates;
    state.prevStates = cur;
    if (!prev || !state.notify || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    const gen = parseTime(snap.generated_at);
    runs.forEach(function (r) {
      if (r.state !== 'attention' && r.state !== 'failed') return;
      const before = prev.get(r.run_id);
      if (before === r.state) return;
      if (before === undefined && r.state === 'failed') {
        const c = parseTime(r.completed_at);
        if (!isFinite(c) || !isFinite(gen) || gen - c > 5 * 60000) return;
      }
      try {
        const info = r.state === 'attention' ? attentionInfo(r) : { diag: '' };
        new Notification(r.state === 'attention' ? r.ticket_id + ' precisa de você' : r.ticket_id + ' falhou', {
          body: (r.title || '') + (info.diag ? ' — ' + String(info.diag).slice(0, 120) : ''), tag: 'line-live-' + r.run_id,
        });
      } catch (e) { /* ignora */ }
    });
  }

  /* =====================================================================
   * 9. Dados em tempo real: SSE com fallback para polling
   * ===================================================================== */

  function setConn(c) { if (state.conn !== c) { state.conn = c; if (state.snapshot) renderBanner(state.view || deriveView(state.snapshot, state.filters), state.snapshot); } renderConn(Date.now()); }

  function newer(next) {
    // Compara por generated_at (a versão pode reiniciar com o servidor).
    const cur = state.snapshot;
    if (!cur) return true;
    const ga = parseTime(next.generated_at), gb = parseTime(cur.generated_at);
    return !(isFinite(ga) && isFinite(gb)) || ga >= gb;
  }

  function applySnapshot(data) {
    if (!data || typeof data !== 'object' || !Array.isArray(data.runs)) { if (root.console) console.warn('[LineLive] snapshot inválido ignorado'); return; }
    if (!newer(data)) return;
    state.snapshot = data;
    state.receivedAt = Date.now();
    try { checkNotifications(data); } catch (e) { /* ignora */ }
    render();
  }

  function fetchSnapshot() {
    const ctl = new AbortController();
    conn.abort = ctl;
    const timer = setTimeout(function () { ctl.abort(); }, FETCH_TIMEOUT_MS);
    return fetch(SNAPSHOT_URL, { signal: ctl.signal, headers: { Accept: 'application/json' }, cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .finally(function () { clearTimeout(timer); });
  }

  function closeStream() {
    if (conn.es) { try { conn.es.close(); } catch (e) { /* ignora */ } conn.es = null; }
  }

  function stopPolling() {
    conn.polling = false;
    clearTimeout(conn.pollTimer); conn.pollTimer = null;
    if (conn.abort) { try { conn.abort.abort(); } catch (e) { /* ignora */ } }
    conn.inflight = false;
  }

  function pollOnce() {
    if (!conn.polling || conn.inflight) return;
    conn.inflight = true;
    fetchSnapshot().then(function (data) {
      if (!conn.polling) return;
      applySnapshot(data);
      setConn('polling');
    }).catch(function () {
      if (!conn.polling) return;
      setConn('offline');
    }).finally(function () {
      conn.inflight = false;
      if (conn.polling) { clearTimeout(conn.pollTimer); conn.pollTimer = setTimeout(pollOnce, POLL_MS); }
    });
  }

  function startPolling() {
    if (conn.polling || document.hidden) return;
    conn.polling = true;
    if (state.conn !== 'offline') setConn('polling');
    pollOnce();
  }

  function scheduleSseRetry() {
    clearTimeout(conn.retryTimer);
    conn.retryTimer = setTimeout(function () { if (!document.hidden && !conn.es) openStream(true); }, SSE_RETRY_MS);
  }

  function openStream(trial) {
    closeStream();
    if (document.hidden) return;
    if (typeof EventSource === 'undefined') { startPolling(); return; }
    let es;
    try { es = new EventSource(STREAM_URL); } catch (e) { startPolling(); scheduleSseRetry(); return; }
    conn.es = es;
    if (!trial) setConn('reconnecting');
    es.addEventListener('snapshot', function (ev) {
      if (conn.es !== es) return;
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      conn.failures = 0;
      clearTimeout(conn.retryTimer);
      if (conn.polling) stopPolling();
      setConn('live');
      applySnapshot(data);
    });
    es.onerror = function () {
      if (conn.es !== es) return;
      conn.failures++;
      if (trial || conn.failures >= 3 || es.readyState === 2) {
        closeStream();
        startPolling();
        scheduleSseRetry();
      } else if (!conn.polling) {
        setConn('reconnecting');
      }
    };
  }

  function connect() {
    conn.failures = 0;
    stopPolling();
    closeStream();
    clearTimeout(conn.retryTimer);
    fetchSnapshot().then(applySnapshot).catch(function () { /* o stream/polling assumem */ });
    openStream(false);
  }

  function onVisibility() {
    if (document.hidden) {
      closeStream(); stopPolling(); clearTimeout(conn.retryTimer);
      return;
    }
    setConn('reconnecting');
    connect();
    tick();
  }

  /* =====================================================================
   * 10. Eventos de UI
   * ===================================================================== */

  function doCopy(btn) {
    const v = btn.getAttribute('data-copy');
    const done = function () {
      const old = btn.textContent;
      btn.textContent = 'Copiado';
      setTimeout(function () { btn.textContent = old === 'Copiado' ? 'Copiar' : old; }, 1300);
    };
    const fallback = function () {
      const ta = h('textarea', { style: 'position:fixed;opacity:0;left:-999px', readonly: true });
      ta.value = v; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* ignora */ }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done, fallback); else fallback();
  }

  function moveLane(dir) {
    const lanes = Array.from(el.lanes.querySelectorAll('.lane[data-open-run]'));
    if (!lanes.length) return;
    const cur = document.activeElement && document.activeElement.closest ? document.activeElement.closest('.lane[data-open-run]') : null;
    let i = cur ? lanes.indexOf(cur) + dir : (dir > 0 ? 0 : lanes.length - 1);
    i = Math.max(0, Math.min(lanes.length - 1, i));
    lanes[i].focus();
    lanes[i].scrollIntoView({ block: 'nearest' });
  }

  function bindUi() {
    let searchTimer = null;
    el.search.addEventListener('input', function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { state.filters.q = el.search.value; state.doneLimit = DONE_PAGE; render(); }, 120);
    });
    el.toggleDone.addEventListener('change', function () { state.filters.showDone = el.toggleDone.checked; filtersChanged(); });
    el.notifyBtn.addEventListener('click', toggleNotify);
    el.doneToggle.addEventListener('click', function () { state.doneCollapsed = !state.doneCollapsed; if (state.view) renderDone(state.view, state.snapshot); });
    el.doneMore.addEventListener('click', function () { state.doneLimit += DONE_PAGE; if (state.view) renderDone(state.view, state.snapshot); });

    document.addEventListener('click', function (e) {
      const t = e.target;
      if (!(t instanceof Element)) return;
      const copy = t.closest('[data-copy]');
      if (copy) { doCopy(copy); return; }
      if (t.closest('[data-close-drawer]')) { closeDrawer(); return; }
      const chip = t.closest('#project-chips [data-project]');
      if (chip) { state.filters.project = chip.dataset.project; filtersChanged(); return; }
      if (t.closest('[data-clear-filters]')) {
        state.filters.project = 'all'; state.filters.q = ''; el.search.value = ''; filtersChanged(); return;
      }
      if (t.closest('a')) return;
      if (state.drawer.open) return;
      const open = t.closest('[data-open-run]');
      if (open) {
        const sel = root.getSelection ? String(root.getSelection()) : '';
        if (sel.length > 0) return;
        openDrawer(open.dataset.openRun, open.dataset.stage || null, open);
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tg = e.target;
      const typing = tg && (/^(INPUT|TEXTAREA|SELECT)$/.test(tg.tagName) || tg.isContentEditable);
      if (state.drawer.open) {
        if (e.key === 'Escape') { e.preventDefault(); closeDrawer(); }
        else if (e.key === 'Tab') trapTab(e);
        return;
      }
      if (typing) {
        if (e.key === 'Escape' && tg === el.search) { el.search.value = ''; state.filters.q = ''; render(); el.search.blur(); }
        return;
      }
      if (e.key === '/') { e.preventDefault(); el.search.focus(); el.search.select(); }
      else if (e.key === 'j') moveLane(1);
      else if (e.key === 'k') moveLane(-1);
      else if ((e.key === 'Enter' || e.key === ' ') && tg && tg.closest && tg.tagName !== 'BUTTON' && tg.tagName !== 'A') {
        const o = tg.closest('[data-open-run]');
        if (o && o === tg) { e.preventDefault(); openDrawer(o.dataset.openRun, o.dataset.stage || null, o); }
      }
    });

    document.addEventListener('visibilitychange', onVisibility);
    root.addEventListener('online', function () { if (!document.hidden) { setConn('reconnecting'); connect(); } });
    root.addEventListener('offline', function () { setConn('offline'); });
  }

  /* =====================================================================
   * 11. Boot
   * ===================================================================== */

  loadFilters();
  bindUi();
  renderNotifyBtn();
  renderConn(Date.now());
  setInterval(tick, 1000);
  if (!document.hidden) connect();
  else fetchSnapshot().then(applySnapshot).catch(function () { /* reabre ao ficar visível */ });
  root.LineLive._state = state;
})(typeof window !== 'undefined' ? window : globalThis);
