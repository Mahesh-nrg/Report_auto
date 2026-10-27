/* pwndoc-ai: report tools of the Finish drawer: QA pass, executive summary, library save. */
import { api } from './ai-api.js';
import { btn, chip, h, note, plural, section } from './ai-dom.js';
import { root } from './ai-shell.js';
import { S, ui } from './ai-state.js';
import { MAX_LISTED, SEVERITY_LABEL, SEVERITY_TONE, plain } from './ai-util.js';
import { llmMissing } from './ai-widgets.js';

const render = (focusKey) => ui.renderBody('finish', focusKey);
const auditPath = (auditId, tail) => '/audits/' + encodeURIComponent(auditId) + tail;

/* ---------- QA pass ---------- */
function normQa(d) {
  const list = Array.isArray(d && d.issues) ? d.issues : [];
  const issues = list.filter((i) => i && typeof i === 'object').map((i) => ({
    severity: SEVERITY_LABEL[i.severity] ? i.severity : 'info', code: String(i.code || ''), message: String(i.message || ''),
    title: String(i.finding_title || ''), field: String(i.field || ''),
  }));
  const c = (d && d.counts) || {};
  const count = (k) => (Number.isInteger(c[k]) ? c[k] : issues.filter((i) => i.severity === k).length);
  return { status: 'ready', issues, counts: { error: count('error'), warning: count('warning'), info: count('info') }, error: '', open: false };
}

export async function loadQa() {
  const T = S.tools;
  T.qa = { status: 'loading', issues: [], counts: null, error: '', open: false };
  render();
  try {
    T.qa = normQa(await api(auditPath(T.auditId, '/qa')));
  } catch (e) {
    T.qa = { status: 'error', issues: [], counts: null, error: e.message, open: false };
  }
  render();
}

function issueRow(i) {
  const where = [i.title, i.field].filter(Boolean).join(' · ');
  return h('li', { class: 'ai-issue' },
    chip(SEVERITY_TONE[i.severity], SEVERITY_LABEL[i.severity]),
    h('span', { class: 'ai-issue-text' }, i.message, where ? h('small', {}, where) : null));
}

function qaResult(q) {
  const c = q.counts;
  const summary = h('div', { class: 'ai-row' },
    chip(c.error ? 'bad' : 'ok', plural(c.error, 'error')), chip(c.warning ? 'warn' : 'ok', plural(c.warning, 'warning')),
    chip('mute', plural(c.info, 'note')));
  if (!q.issues.length) return [summary, h('p', { class: 'ai-quiet' }, 'No problems found. This check does not block finishing.')];
  const toggleBtn = h('button', {
    type: 'button', class: 'ai-btn ai-link', 'aria-expanded': String(q.open), 'data-fk': 'qa-toggle',
    onclick: () => { S.tools.qa = Object.assign({}, S.tools.qa, { open: !S.tools.qa.open }); render(); },
  }, (q.open ? 'Hide' : 'Show') + ' issues (' + q.issues.length + ')');
  const shown = q.issues.slice(0, MAX_LISTED);
  const list = q.open ? h('ul', { class: 'ai-issues' }, shown.map(issueRow),
    q.issues.length > shown.length ? h('li', { class: 'ai-quiet' }, 'and ' + (q.issues.length - shown.length) + ' more') : null) : null;
  return [summary, h('p', { class: 'ai-quiet' }, 'These do not block finishing; fix what you can first.'), toggleBtn, list];
}

export function qaSection() {
  const q = S.tools.qa;
  const head = h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', 'Re-check', loadQa, { disabled: q.status === 'loading' ? true : null, 'data-fk': 'qa-recheck' }));
  let kids;
  if (q.status === 'loading' || q.status === 'idle') kids = [h('p', { class: 'ai-quiet' }, 'Checking the report…')];
  else if (q.status === 'error') kids = [note('warn', 'The report checks could not run: ' + q.error + ' You can still finish the report.')];
  else kids = qaResult(q);
  return section('Report check', kids, head);
}

/* ---------- executive summary ---------- */
const RICH_TAGS = new Set(['p', 'ul', 'ol', 'li', 'strong', 'b', 'em', 'i', 'br', 'blockquote', 'h1', 'h2', 'h3', 'h4']);
const RICH_DROP = new Set(['script', 'style', 'img', 'iframe', 'object', 'embed', 'svg', 'link', 'meta']);
const RICH_MAX_DEPTH = 12;

// Draws the model's HTML as inert DOM nodes: allow-listed tags only, no attributes, no images.
function richNodes(node, depth) {
  const out = [];
  for (const n of node.childNodes) {
    if (n.nodeType === Node.TEXT_NODE) out.push(document.createTextNode(n.nodeValue));
    else if (n.nodeType === Node.ELEMENT_NODE) out.push(...richElement(n, depth));
  }
  return out;
}
function richElement(n, depth) {
  const tag = n.tagName.toLowerCase();
  if (RICH_DROP.has(tag) || depth > RICH_MAX_DEPTH) return [];
  const kids = richNodes(n, depth + 1);
  return RICH_TAGS.has(tag) ? [h(tag, {}, kids)] : kids;
}
const renderRich = (html) => h('div', { class: 'ai-rich', tabindex: '0', 'aria-label': 'Draft text' },
  richNodes(new DOMParser().parseFromString(String(html), 'text/html').body, 0));

function normSummary(d) {
  return {
    html: typeof (d && d.summary_html) === 'string' ? d.summary_html : '', written: !!(d && d.written),
    target: d && typeof d.target === 'string' ? d.target : '', model: d && typeof d.model === 'string' ? d.model : '',
    stats: d && d.stats && typeof d.stats === 'object' ? d.stats : null, copied: '',
    exists: !!(d && d.exists), error: d && typeof d.error === 'string' ? d.error : '', confirm: false,
  };
}

async function draftSummary() {
  const T = S.tools;
  if (T.summary.busy) return;
  T.summary = { busy: true, error: '', data: null };
  render();
  try {
    T.summary = { busy: false, error: '', data: normSummary(await api(auditPath(T.auditId, '/summary'), { method: 'POST' })) };
  } catch (e) {
    T.summary = { busy: false, error: e.message, data: null };
  }
  render();
}

const setSummaryData = (patch, focusKey) => {
  const T = S.tools;
  T.summary = Object.assign({}, T.summary, { data: Object.assign({}, T.summary.data, patch) });
  render(focusKey);
};

// Replaces the audit's existing summary with the draft already on screen (no new LLM call).
async function replaceSummary() {
  const T = S.tools;
  const shown = T.summary.data;
  if (T.summary.busy || !shown) return;
  T.summary = { busy: true, error: '', data: shown };
  render();
  try {
    const body = { overwrite: true, summary_html: shown.html };
    T.summary = { busy: false, error: '', data: normSummary(await api(auditPath(T.auditId, '/summary'), { method: 'POST', body })) };
  } catch (e) {
    T.summary = { busy: false, error: e.message, data: Object.assign({}, shown, { confirm: false }) };
  }
  render();
}

async function copyWithClipboardApi(html, text) {
  try {
    if (window.ClipboardItem && navigator.clipboard.write) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }), 'text/plain': new Blob([text], { type: 'text/plain' }),
      })]);
    } else await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e2) { return false; }
  }
}

// navigator.clipboard only exists in secure contexts; the gateway is often reached over plain HTTP.
function copyWithExecCommand(text) {
  const before = root.activeElement;
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  area.remove();
  if (before && before.focus) before.focus({ preventScroll: true });
  return ok;
}

async function copyRich(html) {
  const text = plain(html);
  return (await copyWithClipboardApi(html, text)) || copyWithExecCommand(text);
}

async function copySummary() {
  const ok = await copyRich(S.tools.summary.data.html);
  const said = ok ? 'Copied.' : 'Copy was blocked; select the text and copy it manually.';
  setSummaryData({ copied: ok ? 'ok' : 'fail' }, 'summary-copy');
  ui.announce(said);
}

function statsLine(stats) {
  const by = stats && stats.by_severity;
  if (!by || typeof by !== 'object') return '';
  const parts = Object.entries(by).filter(([, n]) => Number.isInteger(n) && n > 0).map(([k, n]) => k + ' ' + n);
  return plural(Number.isInteger(stats.total) ? stats.total : 0, 'finding') + (parts.length ? ': ' + parts.join(' · ') : '');
}

function replaceControls(d) {
  if (!d.confirm) {
    return h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', 'Replace the existing summary',
      () => setSummaryData({ confirm: true }, 'summary-replace-yes'), { 'data-fk': 'summary-replace' }));
  }
  return h('div', { class: 'ai-stack' },
    note('warn', 'Replace the audit’s executive summary with this draft? The old text is kept in the gateway’s backups, not in PwnDoc.'),
    h('div', { class: 'ai-row' },
      btn('ai-primary ai-sm', 'Yes, replace it', replaceSummary, { 'data-fk': 'summary-replace-yes' }),
      btn('ai-ghost ai-sm', 'Cancel', () => setSummaryData({ confirm: false }, 'summary-replace'), { 'data-fk': 'summary-replace-no' })));
}

function summaryStatus(d) {
  if (d.written) {
    return note('ok', h('strong', {}, 'Written into the report'), d.target ? ' (' + d.target + '). ' : '. ', 'Reload the page to see it in the editor.');
  }
  if (d.exists) {
    return note('warn', h('strong', {}, 'This audit already has an executive summary, so nothing was overwritten. '),
      'Review the draft below; you can replace the existing text with it.');
  }
  if (d.target) {
    return note('bad', h('strong', {}, 'Found the summary field (' + d.target + ') but could not save into it. '),
      d.error ? d.error + '. ' : '', 'Copy the text below into the report yourself.');
  }
  return note('warn', 'This audit has no executive summary field to write into. Copy the text below into the report yourself.');
}

function summaryResult(r) {
  const d = r.data;
  const out = [summaryStatus(d)];
  const meta = [statsLine(d.stats), d.model ? 'Model: ' + d.model : ''].filter(Boolean).join(' · ');
  if (meta) out.push(h('p', { class: 'ai-item-meta' }, meta));
  out.push(renderRich(d.html));
  if (d.exists) out.push(replaceControls(d));
  if (!d.written) {
    out.push(h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', 'Copy', copySummary, { 'data-fk': 'summary-copy' }),
      h('span', { class: 'ai-quiet' },
        d.copied === 'ok' ? 'Copied.' : d.copied === 'fail' ? 'Copy was blocked; select the text and copy it manually.' : '')));
  }
  return out;
}

/* ---------- vulnerability library ---------- */
async function saveLibrary() {
  const T = S.tools;
  if (T.library.busy) return;
  T.library = { busy: true, error: '', data: null };
  render();
  try {
    const d = await api(auditPath(T.auditId, '/library'), { method: 'POST' });
    const skipped = Array.isArray(d && d.skipped) ? d.skipped.filter((x) => x && typeof x === 'object') : [];
    const created = Array.isArray(d && d.created) ? d.created.map(String) : [];
    T.library = { busy: false, error: '', data: { created, skipped: skipped.map((x) => ({ title: String(x.title || ''), reason: String(x.reason || '') })) } };
  } catch (e) {
    T.library = { busy: false, error: e.message, data: null };
  }
  render();
}

function libraryResult(d) {
  const out = [d.created.length
    ? note('ok', h('strong', {}, plural(d.created.length, 'finding') + ' saved to the library. '), d.created.slice(0, MAX_LISTED).join(', '))
    : note('warn', 'Nothing new was saved.')];
  if (d.skipped.length) {
    out.push(h('ul', { class: 'ai-issues' }, d.skipped.slice(0, MAX_LISTED).map((x) =>
      h('li', { class: 'ai-issue' }, chip('mute', 'Skipped'), h('span', { class: 'ai-issue-text' }, x.title || '(untitled)', h('small', {}, x.reason))))));
  }
  return out;
}

export function toolsSection() {
  const { summary, library } = S.tools;
  const summaryOff = summary.busy || llmMissing();
  return h('div', { class: 'ai-stack' },
    section('Executive summary',
      h('p', { class: 'ai-quiet' }, 'Drafts a management summary from the findings. It is written into the report when the audit has an empty executive summary field; an existing summary is never replaced without asking.'),
      h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', summary.busy ? 'Drafting…' : 'Draft executive summary', draftSummary,
        { disabled: summaryOff ? true : null, 'data-fk': 'summary-draft' })),
      summary.error ? note('bad', summary.error) : null,
      summary.data ? summaryResult(summary) : null),
    section('Vulnerability library',
      h('p', { class: 'ai-quiet' }, 'Saves the generic wording of this audit’s findings (title, description, remediation, references) to PwnDoc’s vulnerability library, which every PwnDoc user can read. Observations are never saved. Findings already there, or that still name the client, a host, an IP address or an e-mail, are skipped.'),
      h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', library.busy ? 'Saving…' : 'Save findings to the library', saveLibrary,
        { disabled: library.busy ? true : null, 'data-fk': 'library-save' })),
      library.error ? note('bad', library.error) : null,
      library.data ? libraryResult(library.data) : null));
}
