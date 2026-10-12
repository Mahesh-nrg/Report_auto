/* pwndoc-ai: "Update with AI" dock + AI Settings, injected into the stock PwnDoc SPA.
 * Vanilla JS, no dependencies. All UI lives in a Shadow DOM so Quasar CSS cannot interfere.
 * Data from the gateway (finding titles, diffs) is only ever rendered via textContent. */
(function () {
  'use strict';
  if (window.__pwndocAiLoaded) return;
  window.__pwndocAiLoaded = true;

  const SCRIPT_SRC = document.currentScript && document.currentScript.src;
  const API = '/__ai/api';
  const MASK = '********';
  const POLL_MS = 1500;
  const JOB_KEY = 'pwndoc-ai:job';
  const TERMINAL = new Set(['done', 'completed', 'complete', 'finished', 'cancelled', 'canceled', 'failed', 'error']);
  const FIELDS = [
    ['title', 'Title'], ['description', 'Description'], ['observation', 'Observation'],
    ['poc', 'Proof of concept'], ['scope', 'Scope'], ['remediation', 'Remediation'],
    ['references', 'References'], ['customFields', 'Custom fields'],
  ];
  const FIELD_LABEL = Object.fromEntries(FIELDS);
  const STATUS_LABEL = {
    pending: 'Queued', queued: 'Queued', running: 'Working', proposed: 'Ready to review',
    applied: 'Applied', failed: 'Failed', error: 'Failed', skipped: 'No changes',
    unchanged: 'No changes', cancelled: 'Cancelled', canceled: 'Cancelled', done: 'Done',
  };
  const NOT_APPLIABLE = new Set(['applied', 'failed', 'error', 'cancelled', 'canceled', 'running', 'pending', 'queued', 'skipped', 'unchanged']);

  /* ---------- tiny DOM helpers ---------- */
  function add(el, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) kid.forEach((k) => add(el, k));
    else el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'value' || k === 'checked') el[k] = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    add(el, kids);
    return el;
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  const ICONS = {
    close: ['M6 6l12 12', 'M18 6L6 18'],
    chev: ['M8 10l4 4 4-4'],
    spark: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z'],
    sliders: ['M4 7h9', 'M17 7h3', 'M4 17h3', 'M11 17h9', 'M15 5v4', 'M9 15v4'],
    check: ['M5 12.5l4.5 4.5L19 7.5'],
    trash: ['M5 7h14', 'M10 7V5h4v2', 'M7 7l1 12h8l1-12'],
    upload: ['M12 16V5', 'M7.5 9.5L12 5l4.5 4.5', 'M5 19h14'],
  };
  function icon(name) {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('class', 'ai-icon');
    s.setAttribute('aria-hidden', 'true');
    for (const d of ICONS[name]) {
      const p = document.createElementNS(SVGNS, 'path');
      p.setAttribute('d', d);
      s.append(p);
    }
    return s;
  }
  const btn = (cls, label, onclick, extra) =>
    h('button', Object.assign({ type: 'button', class: 'ai-btn ' + cls, onclick }, extra), label);

  /* ---------- API ---------- */
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  function detailText(data) {
    const d = data && (data.detail !== undefined ? data.detail : data.message);
    if (Array.isArray(d)) return d.map((x) => (x && x.msg) || String(x)).join('; ');
    return d == null ? '' : typeof d === 'string' ? d : JSON.stringify(d);
  }
  async function api(path, opts) {
    const { method = 'GET', body, form } = opts || {};
    const headers = {};
    let payload;
    if (method !== 'GET') headers['X-Requested-With'] = 'pwndoc-ai';
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    if (form) payload = form;
    let res;
    try {
      res = await fetch(API + path, { method, headers, body: payload, credentials: 'same-origin' });
    } catch (e) {
      throw new ApiError('Cannot reach the AI gateway', 0);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON body */ }
    if (!res.ok) throw new ApiError(detailText(data) || res.statusText || 'Request failed', res.status);
    return data;
  }

  /* ---------- text helpers ---------- */
  function plain(v) {
    if (Array.isArray(v)) return v.map(plain).join('\n');
    if (v == null) return '';
    if (typeof v === 'object') return JSON.stringify(v, null, 1);
    let s = String(v);
    if (/<[a-z][\s\S]*>/i.test(s)) {
      s = s.replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, '').replace(/<img\b[^>]*>/gi, ' [image] ').replace(/<\/(p|div|li|h[1-6]|tr)>|<br\s*\/?>/gi, '\n');
      s = new DOMParser().parseFromString(s, 'text/html').body.textContent || '';
    }
    return s.replace(/\n{3,}/g, '\n\n').trim();
  }
  const fmtTokens = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n || 0));
  const isTerminal = (s) => TERMINAL.has(String(s || '').toLowerCase());
  const shortId = (id) => (id ? String(id).slice(-6) : '');

  function normChanges(raw) {
    if (!raw) return [];
    const list = Array.isArray(raw)
      ? raw.map((c) => [c.field || c.label || c.name || '', c])
      : Object.entries(raw);
    return list
      .map(([field, c]) => {
        const isObj = c && typeof c === 'object' && !Array.isArray(c);
        const before = isObj ? (c.before !== undefined ? c.before : c.old) : '';
        const after = isObj ? (c.after !== undefined ? c.after : c.new) : c;
        return { field, label: FIELD_LABEL[field] || field, before: plain(before), after: plain(after) };
      })
      .filter((c) => c.before !== c.after);
  }
  function normJob(j) {
    const rawItems = j.items || j.findings || j.results || [];
    const items = rawItems.map((it) => {
      const changes = normChanges(it.changes || it.diff);
      let status = String(it.status || 'pending').toLowerCase();
      if (it.applied === true) status = 'applied';
      return {
        id: it.finding_id || it.id || it._id,
        title: it.title || '(untitled finding)',
        status, changes, error: it.error || '', usage: it.usage || null,
      };
    });
    return {
      id: j.id || j.job_id, status: String(j.status || 'running').toLowerCase(), items,
      usage: j.usage || null, error: j.error || '', auditId: j.audit_id || null,
    };
  }
  const appliable = (it) => it.changes.length > 0 && (it.status === 'proposed' || !NOT_APPLIABLE.has(it.status));

  /* ---------- state ---------- */
  const freshUpd = () => ({
    auditId: null, findings: null, loadingFindings: false, scope: 'all', selected: new Set(),
    fields: new Set(FIELDS.map((f) => f[0])), mode: 'improve', auto: false,
    job: null, sig: '', error: '', note: '', starting: false, applying: false,
    seen: new Set(), accept: new Set(), expanded: new Set(), pollFails: 0, appliedAny: false,
  });
  const S = {
    status: null, route: { kind: 'other' }, open: null, upd: freshUpd(),
    set: { data: null, loading: false, error: '' },
  };
  const isAdmin = () => !!(S.status && S.status.authenticated && (S.status.is_admin === true ||
    (S.status.is_admin === undefined && String(S.status.role).toLowerCase() === 'admin')));

  /* ---------- shadow root ---------- */
  const host = h('div', { id: 'pwndoc-ai-host' });
  host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483000;visibility:hidden';
  const root = host.attachShadow({ mode: 'open' });
  const styleEl = h('style');
  const dockMount = h('div');
  const layer = h('div');
  root.append(styleEl, dockMount, layer);

  async function loadStyles() {
    const link = document.querySelector('link[href*="inject.css"]');
    const urls = [
      link && link.href,
      SCRIPT_SRC && SCRIPT_SRC.replace(/inject\.js(\?.*)?$/, 'inject.css'),
      '/__ai/static/inject.css',
    ].filter(Boolean);
    for (const u of urls) {
      try {
        const r = await fetch(u, { credentials: 'same-origin' });
        if (r.ok) { styleEl.textContent = await r.text(); return; }
      } catch (e) { /* try next */ }
    }
    console.warn('pwndoc-ai: could not load inject.css'); // eslint-disable-line no-console
  }
  function syncTheme() {
    const b = document.body;
    const dark = b.classList.contains('body--dark') ||
      (!b.classList.contains('body--light') && window.matchMedia('(prefers-color-scheme: dark)').matches);
    host.classList.toggle('ai-dark', dark);
  }

  /* ---------- focus helpers ---------- */
  function keepFocus(fn) {
    const a = root.activeElement;
    const key = a && a.getAttribute && a.getAttribute('data-fk');
    const scroller = layer.querySelector('.ai-body');
    const top = scroller ? scroller.scrollTop : 0;
    fn();
    const sc = layer.querySelector('.ai-body');
    if (sc) sc.scrollTop = top;
    if (key) {
      const n = root.querySelector('[data-fk="' + CSS.escape(key) + '"]');
      if (n && n !== root.activeElement) n.focus({ preventScroll: true });
    }
  }
  let drawerEl = null;
  let bodyEl = null;
  let footEl = null;
  let titleEl = null;
  let returnFocus = null;
  const focusables = () =>
    Array.from(drawerEl.querySelectorAll('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'))
      .filter((n) => n.getClientRects().length > 0);
  document.addEventListener('keydown', (e) => {
    if (!S.open || !drawerEl) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDrawer(); return; }
    if (e.key !== 'Tab') return;
    const f = focusables();
    if (!f.length) return;
    const active = root.activeElement;
    const inside = drawerEl.contains(active);
    if (e.shiftKey && (active === f[0] || !inside)) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (active === f[f.length - 1] || !inside)) { e.preventDefault(); f[0].focus(); }
  }, true);

  /* ---------- routing ---------- */
  const AUDIT_RE = /^\/audits\/([0-9a-f]{24})(?:\/findings\/([0-9a-f]{24}))?(?:\/|$)/i;
  function parseRoute() {
    const p = location.pathname;
    if (p.startsWith('/login')) return { kind: 'login' };
    const m = AUDIT_RE.exec(p);
    if (m) return { kind: 'audit', auditId: m[1], findingId: m[2] || null };
    return { kind: 'other', settings: p.startsWith('/settings') };
  }
  const sameRoute = (a, b) =>
    a.kind === b.kind && a.auditId === b.auditId && a.findingId === b.findingId && !!a.settings === !!b.settings;
  function onRoute() {
    const next = parseRoute();
    const prev = S.route;
    if (sameRoute(prev, next)) return;
    S.route = next;
    if (!S.status || !S.status.authenticated || prev.kind === 'login') refreshStatus();
    const U = S.upd;
    const jobActive = U.job && !isTerminal(U.job.status);
    if (next.kind !== 'audit' || next.auditId !== U.auditId) {
      if (S.open === 'update' && !jobActive) closeDrawer();
      if (!jobActive) S.upd = freshUpd();
    } else if (!U.job) {
      U.scope = next.findingId ? 'one' : 'all';
    }
    renderDock();
  }
  for (const m of ['pushState', 'replaceState']) {
    const orig = history[m];
    history[m] = function () {
      const r = orig.apply(this, arguments);
      window.dispatchEvent(new Event('pwndoc-ai:nav'));
      return r;
    };
  }
  window.addEventListener('pwndoc-ai:nav', onRoute);
  window.addEventListener('popstate', onRoute);
  let lastUrl = location.pathname;
  setInterval(() => { if (location.pathname !== lastUrl) { lastUrl = location.pathname; onRoute(); } }, 500);

  /* ---------- status ---------- */
  async function refreshStatus() {
    try { S.status = await api('/status'); } catch (e) { S.status = { authenticated: false, error: e.message }; }
    renderDock();
  }

  /* ---------- dock ---------- */
  function renderDock() {
    const st = S.status;
    const r = S.route;
    if (!st || !st.authenticated || r.kind === 'login') { dockMount.replaceChildren(); return; }
    const kids = [];
    const job = S.upd.job;
    const busy = job && !isTerminal(job.status);
    if (r.kind === 'audit') {
      const done = busy ? job.items.filter((i) => !['pending', 'queued', 'running'].includes(i.status)).length : 0;
      kids.push(h('button', {
        type: 'button', class: 'ai-dock-main' + (busy ? ' ai-busy' : ''), 'data-fk': 'dock-update',
        onclick: () => openDrawer('update'), 'aria-label': 'Update with AI',
      }, icon('spark'), h('span', { class: 'ai-dock-label' }, busy ? 'AI working ' + done + '/' + job.items.length : 'Update with AI')));
    }
    if (isAdmin()) {
      kids.push(h('button', {
        type: 'button', class: 'ai-dock-gear' + (r.settings ? ' ai-dock-wide' : ''), 'data-fk': 'dock-settings',
        onclick: () => openDrawer('settings'), 'aria-label': 'AI Settings', title: 'AI Settings',
      }, icon('sliders'), r.settings ? h('span', { class: 'ai-dock-label' }, 'AI Settings') : null));
    }
    dockMount.replaceChildren(kids.length ? h('div', { class: 'ai-dock', role: 'group', 'aria-label': 'AI assistant' }, kids) : null);
  }

  /* ---------- drawer shell ---------- */
  function openDrawer(kind) {
    if (S.open) closeDrawer(true);
    returnFocus = root.activeElement || null;
    S.open = kind;
    titleEl = h('h2', { id: 'ai-title', class: 'ai-title' });
    bodyEl = h('div', { class: 'ai-body' });
    footEl = h('div', { class: 'ai-foot' });
    drawerEl = h('aside', { class: 'ai-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'ai-title' },
      h('header', { class: 'ai-head' },
        h('div', {}, h('p', { class: 'ai-kicker' }, kind === 'update' ? 'AI report assistant' : 'Administration'), titleEl),
        h('button', { type: 'button', class: 'ai-x', 'aria-label': 'Close', onclick: () => closeDrawer() }, icon('close'))),
      bodyEl, footEl);
    const backdrop = h('div', { class: 'ai-backdrop', onclick: () => closeDrawer() });
    layer.replaceChildren(backdrop, drawerEl);
    host.style.visibility = 'visible';
    requestAnimationFrame(() => { backdrop.classList.add('ai-open'); drawerEl.classList.add('ai-open'); });
    if (kind === 'update') prepareUpdate(); else prepareSettings();
    const first = focusables()[0] || drawerEl.querySelector('.ai-x');
    if (first) first.focus({ preventScroll: true });
  }
  function closeDrawer(silent) {
    S.open = null;
    layer.replaceChildren();
    drawerEl = bodyEl = footEl = titleEl = null;
    if (!silent && returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
    else if (!silent) { const d = root.querySelector('.ai-dock button'); if (d) d.focus(); }
  }
  function renderBody() {
    if (!S.open || !bodyEl) return;
    keepFocus(() => {
      const view = S.open === 'update' ? viewUpdate() : viewSettings();
      titleEl.textContent = view.title;
      bodyEl.replaceChildren(view.body);
      footEl.replaceChildren(...view.foot);
      footEl.hidden = view.foot.length === 0;
    });
  }

  /* ---------- shared widgets ---------- */
  const note = (tone, ...kids) => h('div', { class: 'ai-note ai-' + tone, role: tone === 'bad' ? 'alert' : 'status' }, kids);
  function choice(type, name, label, checked, onchange, fk, hint) {
    return h('label', { class: 'ai-choice' },
      h('input', { type, name, checked, onchange, 'data-fk': fk }),
      h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }, type === 'checkbox' ? icon('check') : null),
      h('span', { class: 'ai-choice-text' }, label, hint ? h('small', {}, hint) : null));
  }
  function toggle(label, hint, checked, onchange, fk) {
    return h('label', { class: 'ai-switch' },
      h('input', { type: 'checkbox', role: 'switch', checked, onchange, 'data-fk': fk }),
      h('span', { class: 'ai-track', 'aria-hidden': 'true' }),
      h('span', { class: 'ai-choice-text' }, label, hint ? h('small', {}, hint) : null));
  }
  const section = (label, ...kids) => h('section', { class: 'ai-section' }, h('h3', { class: 'ai-label' }, label), kids);
  const notConfigured = () =>
    note('warn', h('strong', {}, 'The LLM is not configured yet. '),
      isAdmin()
        ? [h('span', {}, 'Add a provider, API key and model to continue. '), btn('ai-link', 'Open AI Settings', () => openDrawer('settings'))]
        : 'Ask a PwnDoc administrator to set it up in AI Settings.');

  /* ---------- update with AI ---------- */
  function prepareUpdate() {
    const U = S.upd;
    const r = S.route;
    if (U.job && U.auditId !== r.auditId && isTerminal(U.job.status)) S.upd = freshUpd();
    const V = S.upd;
    if (!V.job) {
      V.auditId = r.auditId;
      if (!V.findings || V.findingsFor !== r.auditId) {
        V.scope = r.findingId ? 'one' : 'all';
        V.selected = new Set(r.findingId ? [r.findingId] : []);
        loadFindings();
      }
    }
    renderBody();
  }
  async function loadFindings() {
    const V = S.upd;
    const auditId = S.route.auditId;
    if (!auditId) return;
    V.loadingFindings = true; V.error = '';
    renderBody();
    try {
      const d = await api('/audits/' + encodeURIComponent(auditId) + '/findings');
      V.findings = (Array.isArray(d) ? d : d.findings || []).map((f) => ({ id: f._id || f.id, title: f.title || '(untitled finding)' }));
      V.findingsFor = auditId;
    } catch (e) {
      V.error = e.message;
      V.findings = [];
    }
    V.loadingFindings = false;
    renderBody();
  }
  function scopeIds() {
    const V = S.upd;
    const all = (V.findings || []).map((f) => f.id);
    if (V.scope === 'one') return S.route.findingId ? [S.route.findingId] : [];
    if (V.scope === 'selected') return all.filter((id) => V.selected.has(id));
    return all;
  }
  function viewUpdate() {
    const V = S.upd;
    const body = h('div', { class: 'ai-stack' });
    const foot = [];
    if (S.status && S.status.llm_configured === false) body.append(notConfigured());
    if (V.error) body.append(note('bad', V.error));
    if (V.note) body.append(note('ok', V.note));
    if (V.job) { jobView(body, foot); return { title: 'AI run', body, foot }; }
    formView(body, foot);
    return { title: 'Update with AI', body, foot };
  }
  function formView(body, foot) {
    const V = S.upd;
    const r = S.route;
    const rerender = () => renderBody();
    const scopes = [];
    if (r.findingId) scopes.push(['one', 'This finding', 'The finding you are editing now']);
    scopes.push(['selected', 'Selected findings', 'Pick any findings from this audit']);
    scopes.push(['all', 'All findings in this audit', V.findings ? V.findings.length + ' findings' : '']);
    body.append(section('Scope', h('div', { class: 'ai-choices' }, scopes.map(([v, l, hint]) =>
      choice('radio', 'scope', l, V.scope === v, () => { V.scope = v; rerender(); }, 'scope-' + v, hint)))));
    if (V.scope === 'selected') body.append(findingPicker());
    if (V.scope === 'one' && V.findings) {
      const f = V.findings.find((x) => x.id === r.findingId);
      body.append(h('p', { class: 'ai-quiet' }, f ? f.title : 'Finding ' + shortId(r.findingId)));
    }
    body.append(section('Fields to update', h('div', { class: 'ai-grid' }, FIELDS.map(([k, l]) =>
      choice('checkbox', 'f-' + k, l, V.fields.has(k), (e) => { e.target.checked ? V.fields.add(k) : V.fields.delete(k); rerender(); }, 'field-' + k)))));
    body.append(section('Mode', h('div', { class: 'ai-choices' }, [
      ['improve', 'Improve existing text', 'Rewrite and expand what is there'],
      ['fill_missing', 'Only fill empty fields', 'Never touch text you already wrote'],
    ].map(([v, l, hint]) => choice('radio', 'mode', l, V.mode === v, () => { V.mode = v; rerender(); }, 'mode-' + v, hint)))));
    body.append(section('Review',
      toggle('Apply automatically', 'Off by default: you review each change before it is written to PwnDoc.', V.auto,
        (e) => { V.auto = e.target.checked; rerender(); }, 'auto')));
    const ids = scopeIds();
    const ready = !V.loadingFindings && ids.length > 0 && V.fields.size > 0 && !V.starting && !(S.status && S.status.llm_configured === false);
    foot.push(h('span', { class: 'ai-foot-info' }, V.loadingFindings ? 'Loading findings\u2026' : ids.length + ' finding' + (ids.length === 1 ? '' : 's') + ' \u00b7 ' + V.fields.size + ' fields'));
    foot.push(btn('ai-primary', V.starting ? 'Starting\u2026' : 'Start', startJob, { disabled: ready ? null : true, 'data-fk': 'start' }));
  }
  function findingPicker() {
    const V = S.upd;
    if (V.loadingFindings || !V.findings) return h('p', { class: 'ai-quiet' }, 'Loading findings\u2026');
    if (!V.findings.length) return h('p', { class: 'ai-quiet' }, 'This audit has no findings yet.');
    const all = V.findings.map((f) => f.id);
    return h('div', { class: 'ai-picker' },
      h('div', { class: 'ai-picker-bar' },
        btn('ai-link', 'Select all', () => { V.selected = new Set(all); renderBody(); }),
        btn('ai-link', 'Select none', () => { V.selected = new Set(); renderBody(); })),
      h('div', { class: 'ai-list', role: 'group', 'aria-label': 'Findings' }, V.findings.map((f) =>
        choice('checkbox', 'sel-' + f.id, f.title, V.selected.has(f.id),
          (e) => { e.target.checked ? V.selected.add(f.id) : V.selected.delete(f.id); renderBody(); }, 'sel-' + f.id))));
  }

  async function startJob() {
    const V = S.upd;
    V.starting = true; V.error = ''; V.note = '';
    renderBody();
    try {
      const resp = await api('/jobs', { method: 'POST', body: {
        audit_id: S.route.auditId, finding_ids: scopeIds(), fields: Array.from(V.fields),
        mode: V.mode, auto_apply: V.auto,
      } });
      const id = resp.id || resp.job_id;
      V.job = resp.items || resp.findings || resp.results ? normJob(resp) : { id, status: 'running', items: [], usage: null };
      V.auditId = S.route.auditId;
      V.seen = new Set(); V.accept = new Set(); V.expanded = new Set(); V.sig = ''; V.pollFails = 0; V.appliedAny = false;
      storeJob({ id: V.job.id, auditId: V.auditId });
      absorb(V.job);
      renderDock();
      pollOnce();
    } catch (e) {
      V.error = e.message;
    }
    V.starting = false;
    renderBody();
  }

  /* ---------- job polling and results ---------- */
  let pollTimer = null;
  function storeJob(v) {
    try { v ? sessionStorage.setItem(JOB_KEY, JSON.stringify(v)) : sessionStorage.removeItem(JOB_KEY); } catch (e) { /* storage unavailable */ }
  }
  function absorb(job) {
    const V = S.upd;
    for (const it of job.items) {
      if (it.status === 'applied') V.appliedAny = true;
      if (appliable(it) && !V.seen.has(it.id)) { V.seen.add(it.id); V.accept.add(it.id); }
    }
  }
  async function pollOnce() {
    clearTimeout(pollTimer);
    const V = S.upd;
    if (!V.job) return;
    try {
      const j = normJob(await api('/jobs/' + encodeURIComponent(V.job.id)));
      V.pollFails = 0;
      const sig = JSON.stringify(j);
      if (sig !== V.sig) { V.sig = sig; V.job = j; absorb(j); renderBody(); renderDock(); }
    } catch (e) {
      if (e.status === 404) {
        V.job = null; V.error = 'This run is no longer available.'; storeJob(null);
        renderBody(); renderDock();
        return;
      }
      if (++V.pollFails > 5) { V.error = 'Lost connection to the gateway. Reopen this panel to resume.'; renderBody(); return; }
    }
    if (V.job && !isTerminal(V.job.status)) pollTimer = setTimeout(pollOnce, POLL_MS);
    else { storeJob(null); renderDock(); }
  }
  async function cancelJob() {
    try { await api('/jobs/' + encodeURIComponent(S.upd.job.id) + '/cancel', { method: 'POST' }); } catch (e) { S.upd.error = e.message; }
    pollOnce();
  }
  async function applyItems(ids) {
    const V = S.upd;
    if (!ids.length) return;
    V.applying = true; V.error = ''; V.note = '';
    renderBody();
    try {
      const r = await api('/jobs/' + encodeURIComponent(V.job.id) + '/apply', { method: 'POST', body: { finding_ids: ids } });
      const n = r && Array.isArray(r.applied) ? r.applied.length : ids.length;
      V.appliedAny = true;
      V.note = n + ' finding' + (n === 1 ? '' : 's') + ' written to PwnDoc.';
      if (r && r.errors && r.errors.length) V.error = r.errors.map((x) => (x && (x.error || x.detail)) || String(x)).join('; ');
    } catch (e) {
      V.error = e.message;
    }
    V.applying = false;
    V.sig = '';
    await pollOnce();
    renderBody();
  }
  function resetRun() {
    clearTimeout(pollTimer);
    storeJob(null);
    const r = S.route;
    S.upd = freshUpd();
    S.upd.auditId = r.auditId;
    renderDock();
    prepareUpdate();
  }

  function jobView(body, foot) {
    const V = S.upd;
    const job = V.job;
    const terminal = isTerminal(job.status);
    const total = job.items.length;
    const finished = job.items.filter((i) => !['pending', 'queued', 'running'].includes(i.status)).length;
    const current = job.items.find((i) => i.status === 'running');
    const usage = job.usage ? (job.usage.input || 0) + (job.usage.output || 0) : 0;
    if (S.upd.auditId !== S.route.auditId) body.append(note('warn', 'This run belongs to a different audit.'));
    if (job.error) body.append(note('bad', job.error));
    const pct = total ? Math.round((finished / total) * 100) : 0;
    body.append(h('div', { class: 'ai-progress' },
      h('div', { class: 'ai-progress-top' },
        h('strong', { class: 'ai-big' }, terminal ? (STATUS_LABEL[job.status] || 'Finished') : 'Refining findings'),
        h('span', { class: 'ai-quiet' }, total ? finished + ' of ' + total : 'Starting\u2026')),
      h('div', { class: 'ai-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct) },
        h('i', { style: 'transform:scaleX(' + pct / 100 + ')' })),
      h('p', { class: 'ai-quiet', 'aria-live': 'polite' },
        current ? 'Working on: ' + current.title : terminal ? 'Run finished.' : 'Waiting for the model\u2026',
        usage ? ' \u00b7 ' + fmtTokens(usage) + ' tokens' : '')));
    const apList = job.items.filter(appliable);
    body.append(h('ul', { class: 'ai-items' }, job.items.map((it) => itemView(it))));
    if (V.appliedAny) {
      body.append(note('ok', h('strong', {}, 'Changes are saved in PwnDoc. '), 'Reload the page to see them in the editor.'));
    }
    if (!terminal) foot.push(btn('ai-ghost', 'Cancel run', cancelJob, { 'data-fk': 'cancel' }));
    if (apList.length) {
      const chosen = apList.filter((i) => V.accept.has(i.id)).map((i) => i.id);
      foot.push(btn('ai-ghost', 'Apply selected (' + chosen.length + ')', () => applyItems(chosen),
        { disabled: chosen.length && !V.applying ? null : true, 'data-fk': 'apply-sel' }));
      foot.push(btn('ai-primary', V.applying ? 'Applying\u2026' : 'Apply all (' + apList.length + ')',
        () => applyItems(apList.map((i) => i.id)), { disabled: V.applying ? true : null, 'data-fk': 'apply-all' }));
    }
    if (V.appliedAny) foot.push(btn(apList.length ? 'ai-ghost' : 'ai-primary', 'Reload page', () => location.reload(), { 'data-fk': 'reload' }));
    if (terminal && !apList.length) foot.push(btn('ai-ghost', 'New run', resetRun, { 'data-fk': 'new-run' }));
  }
  function itemView(it) {
    const V = S.upd;
    const open = V.expanded.has(it.id);
    const can = appliable(it);
    const tone = { applied: 'ok', proposed: 'accent', failed: 'bad', error: 'bad', running: 'accent' }[it.status] || 'mute';
    const head = h('div', { class: 'ai-item-head' },
      can ? h('label', { class: 'ai-pick' },
        h('input', { type: 'checkbox', checked: V.accept.has(it.id), 'aria-label': 'Apply ' + it.title, 'data-fk': 'acc-' + it.id,
          onchange: (e) => { e.target.checked ? V.accept.add(it.id) : V.accept.delete(it.id); renderBody(); } }),
        h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }, icon('check'))) : null,
      h('span', { class: 'ai-item-title' }, it.title),
      h('span', { class: 'ai-chip ai-chip-' + tone }, it.status === 'running' ? h('i', { class: 'ai-pulse' }) : null, STATUS_LABEL[it.status] || it.status),
      it.changes.length ? h('button', {
        type: 'button', class: 'ai-expand' + (open ? ' ai-open' : ''), 'aria-expanded': String(open), 'data-fk': 'exp-' + it.id,
        'aria-label': (open ? 'Hide' : 'Show') + ' changes for ' + it.title,
        onclick: () => { open ? V.expanded.delete(it.id) : V.expanded.add(it.id); renderBody(); },
      }, icon('chev')) : null);
    return h('li', { class: 'ai-item' }, head,
      it.error ? h('p', { class: 'ai-item-error' }, it.error) : null,
      open ? h('div', { class: 'ai-diffs' }, it.changes.map((c) => h('div', { class: 'ai-diff' },
        h('h4', {}, c.label),
        h('div', { class: 'ai-diff-cols' },
          h('div', { class: 'ai-before' }, h('span', {}, 'Before'), h('pre', {}, c.before || '(empty)')),
          h('div', { class: 'ai-after' }, h('span', {}, 'After'), h('pre', {}, c.after || '(empty)')))))) : null);
  }

  /* ---------- AI settings ---------- */
  async function prepareSettings() {
    const D = S.set;
    D.error = '';
    if (!isAdmin()) { renderBody(); return; }
    D.loading = true;
    renderBody();
    try { const d = await api('/settings'); D.data = d.settings || d; } catch (e) { D.error = e.message; }
    D.loading = false;
    renderBody();
  }
  function viewSettings() {
    const D = S.set;
    if (!isAdmin()) return { title: 'AI Settings', body: note('warn', 'Only PwnDoc administrators can change AI settings.'), foot: [] };
    if (D.loading || !D.data) {
      return { title: 'AI Settings', body: D.error ? note('bad', D.error) : h('p', { class: 'ai-quiet' }, 'Loading settings\u2026'), foot: [] };
    }
    return settingsForm(D.data);
  }
  function settingsForm(d) {
    const f = {};
    const msg = h('div', { 'aria-live': 'polite' });
    const show = (tone, text) => { msg.replaceChildren(text ? note(tone, text) : ''); };
    const input = (key, type, value, extra) => {
      f[key] = h('input', Object.assign({ type, class: 'ai-input', value: value == null ? '' : value, name: key, 'data-fk': 'set-' + key }, extra));
      return f[key];
    };
    const field = (label, control, hint) => h('label', { class: 'ai-field' }, h('span', { class: 'ai-field-label' }, label), control, hint ? h('small', {}, hint) : null);
    const modelList = h('datalist', { id: 'ai-models' });
    f.llm_model = input('llm_model', 'text', d.llm_model, { list: 'ai-models', autocomplete: 'off', spellcheck: 'false' });
    f.llm_api_key = input('llm_api_key', 'password', '', {
      autocomplete: 'new-password', placeholder: d.llm_api_key_set ? 'Key stored. Leave blank to keep it.' : 'Paste your API key',
    });
    f.use_web_search = h('input', { type: 'checkbox', checked: !!d.use_web_search, role: 'switch', 'data-fk': 'set-web' });
    f.use_rag = h('input', { type: 'checkbox', checked: !!d.use_rag, role: 'switch', 'data-fk': 'set-rag' });
    f.verify_tls = h('input', { type: 'checkbox', checked: !!d.verify_tls, role: 'switch', 'data-fk': 'set-tls' });
    f.redact = h('textarea', { class: 'ai-input', rows: '3', name: 'redact_field_labels', placeholder: 'One label per line, e.g. Vulnerable URL', 'data-fk': 'set-redact' });
    f.redact.value = (d.redact_field_labels || []).join('\n');
    f.extra = h('textarea', { class: 'ai-input', rows: '4', name: 'extra_instructions', placeholder: 'House style, language, tone, severity conventions\u2026', 'data-fk': 'set-extra' });
    f.extra.value = d.extra_instructions || '';
    const sw = (ctl, label, hint) => h('label', { class: 'ai-switch' }, ctl, h('span', { class: 'ai-track', 'aria-hidden': 'true' }), h('span', { class: 'ai-choice-text' }, label, hint ? h('small', {}, hint) : null));

    const collect = () => ({
      pwndoc_url: f.pwndoc_url.value.trim(), verify_tls: f.verify_tls.checked,
      llm_base_url: f.llm_base_url.value.trim(), llm_api_key: f.llm_api_key.value.trim() || MASK,
      llm_model: f.llm_model.value.trim(), temperature: Number(f.temperature.value),
      max_output_tokens: Number(f.max_output_tokens.value),
      use_web_search: f.use_web_search.checked, web_results: Number(f.web_results.value),
      use_rag: f.use_rag.checked,
      redact_field_labels: f.redact.value.split('\n').map((s) => s.trim()).filter(Boolean),
      extra_instructions: f.extra.value,
    });
    const flagInvalid = (text) => {
      Object.values(f).forEach((el) => el.removeAttribute('aria-invalid'));
      if (!text) return;
      for (const [k, el] of Object.entries(f)) {
        if (text.includes(k) || (k === 'redact' && text.includes('redact_field_labels'))) el.setAttribute('aria-invalid', 'true');
      }
    };
    const setBusy = (v) => { root.querySelectorAll('.ai-foot button, .ai-row .ai-btn').forEach((b) => { b.disabled = v; }); };
    async function save(quiet) {
      flagInvalid('');
      try {
        const resp = await api('/settings', { method: 'PUT', body: collect() });
        S.set.data = resp.settings || resp;
        f.llm_api_key.value = '';
        f.llm_api_key.placeholder = S.set.data.llm_api_key_set ? 'Key stored. Leave blank to keep it.' : 'Paste your API key';
        await refreshStatus();
        if (!quiet) show('ok', 'Settings saved.');
        return true;
      } catch (e) {
        show('bad', e.message); flagInvalid(e.message);
        return false;
      }
    }
    async function saveAndTest() {
      setBusy(true); show('ok', 'Saving and testing\u2026');
      if (await save(true)) {
        try {
          const r = await api('/settings/test', { method: 'POST' });
          const ok = !(r && (r.ok === false || r.success === false));
          show(ok ? 'ok' : 'bad', (r && (r.message || r.reply || r.detail || r.error)) || (ok ? 'Connection OK.' : 'Test failed.'));
        } catch (e) { show('bad', e.message); }
      }
      setBusy(false);
    }
    async function fetchModels() {
      setBusy(true); show('ok', 'Saving and fetching models\u2026');
      if (await save(true)) {
        try {
          const r = await api('/models');
          const names = (Array.isArray(r) ? r : r.models || []).map((m) => (typeof m === 'string' ? m : m.id)).filter(Boolean);
          modelList.replaceChildren(...names.map((n) => h('option', { value: n })));
          show(names.length ? 'ok' : 'warn', names.length ? names.length + ' models found. Start typing in the Model field to pick one.' : 'The provider returned no models.');
        } catch (e) { show('bad', e.message); }
      }
      setBusy(false);
    }

    const ragBox = h('div', { class: 'ai-rag' });
    refreshRag(ragBox);

    const body = h('div', { class: 'ai-stack' },
      msg,
      section('Language model',
        field('Base URL', input('llm_base_url', 'url', d.llm_base_url, { placeholder: 'https://api.openai.com/v1', autocomplete: 'off' }), 'Any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, LM Studio, vLLM\u2026'),
        field('API key', f.llm_api_key, d.llm_api_key_set ? 'A key is saved on the gateway and is never sent back to the browser.' : 'Stored on the gateway only. Not set yet.'),
        field('Model', f.llm_model, null),
        h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', 'Fetch models', fetchModels), btn('ai-ghost ai-sm', 'Test connection', saveAndTest),
          h('small', { class: 'ai-quiet' }, 'Both save the values above first.')),
        h('div', { class: 'ai-two' },
          field('Temperature', input('temperature', 'number', d.temperature, { min: '0', max: '2', step: '0.1' })),
          field('Max output tokens', input('max_output_tokens', 'number', d.max_output_tokens, { min: '256', max: '65536', step: '256' })))),
      section('Research context',
        sw(f.use_web_search, 'Web search (DuckDuckGo)', 'Looks up CWE, OWASP and remediation guidance for each finding.'),
        field('Pages per finding', input('web_results', 'number', d.web_results, { min: '0', max: '10', step: '1' })),
        sw(f.use_rag, 'Local documents (RAG)', 'Use your own notes and standards as extra context.'),
        ragBox),
      section('Privacy',
        field('Hide these custom fields from the model', f.redact, 'Values of fields with these labels are never sent to the LLM and are never changed by it.')),
      section('Style', field('Extra instructions', f.extra, null)),
      section('PwnDoc connection',
        field('PwnDoc URL', input('pwndoc_url', 'url', d.pwndoc_url, { autocomplete: 'off' }), 'Where this gateway reaches the stock PwnDoc server.'),
        sw(f.verify_tls, 'Verify TLS certificate', 'Leave off for the self-signed certificate PwnDoc ships with.')));
    const foot = [
      h('span', { class: 'ai-foot-info' }, 'Changes apply to every user of this gateway.'),
      btn('ai-primary', 'Save settings', async () => { setBusy(true); await save(false); setBusy(false); }, { 'data-fk': 'save' }),
    ];
    return { title: 'AI Settings', body: h('div', {}, body, modelList), foot };
  }

  async function refreshRag(box) {
    const draw = (kids) => box.replaceChildren(...kids);
    try {
      const d = await api('/rag/documents');
      const docs = Array.isArray(d) ? d : d.documents || [];
      if (!Array.isArray(d) && d.available === false) {
        draw([note('warn', 'Local document search is not installed on the gateway. Run: pip install "pwndoc-ai[rag]"')]);
        return;
      }
      const fileInput = h('input', { type: 'file', accept: '.pdf,.txt,.md,.docx,.json', class: 'ai-file', 'aria-label': 'Document to index', 'data-fk': 'rag-file' });
      const status = h('span', { class: 'ai-quiet', 'aria-live': 'polite' });
      const upload = btn('ai-ghost ai-sm', [icon('upload'), 'Upload'], async () => {
        if (!fileInput.files.length) { status.textContent = 'Choose a file first.'; return; }
        const form = new FormData();
        form.append('file', fileInput.files[0]);
        status.textContent = 'Indexing\u2026';
        try { await api('/rag/documents', { method: 'POST', form }); refreshRag(box); } catch (e) { status.textContent = e.message; }
      });
      draw([
        docs.length
          ? h('ul', { class: 'ai-docs' }, docs.map((doc) => h('li', {},
            h('span', { class: 'ai-doc-name' }, doc.name, h('small', {}, (doc.chunks != null ? doc.chunks + ' chunks' : ''))),
            h('button', { type: 'button', class: 'ai-x ai-x-sm', 'aria-label': 'Delete ' + doc.name, 'data-fk': 'del-' + doc.name,
              onclick: async () => {
                try { await api('/rag/documents/' + encodeURIComponent(doc.name), { method: 'DELETE' }); } catch (e) { status.textContent = e.message; return; }
                refreshRag(box);
              } }, icon('trash')))))
          : h('p', { class: 'ai-quiet' }, 'No documents indexed yet. PDF, DOCX, Markdown, text and JSON are supported.'),
        h('div', { class: 'ai-row' }, fileInput, upload), status,
      ]);
    } catch (e) {
      draw([note('bad', e.message)]);
    }
  }

  /* ---------- boot ---------- */
  async function boot() {
    document.body.append(host);
    syncTheme();
    new MutationObserver(syncTheme).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncTheme);
    await loadStyles();
    host.style.visibility = 'visible';
    S.route = parseRoute();
    if (S.route.kind === 'audit') S.upd.scope = S.route.findingId ? 'one' : 'all';
    await refreshStatus();
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(JOB_KEY) || 'null'); } catch (e) { /* ignore */ }
    if (saved && saved.id && S.status && S.status.authenticated && saved.auditId === S.route.auditId) {
      S.upd.job = { id: saved.id, status: 'running', items: [], usage: null };
      S.upd.auditId = saved.auditId;
      pollOnce();
    }
  }
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
