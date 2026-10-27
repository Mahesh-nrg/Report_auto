/* pwndoc-ai: "Update with AI" drawer: scope form, run progress, review and apply. */
import { api } from './ai-api.js';
import { btn, choice, h, icon, note, plural, section, toggle } from './ai-dom.js';
import { S, freshUpd, storeJob, timers, ui } from './ai-state.js';
import {
  FIELDS, POLL_MS, POLL_TIMEOUT_MS, STATUS_LABEL, appliable, countFinished, fmtTokens, isTerminal, normJob,
  pollFailure, shortId,
} from './ai-util.js';
import { itemMeta, llmMissing, notConfigured, primaryModel, progressCard, statusChip } from './ai-widgets.js';

const render = (focusKey) => ui.renderBody('update', focusKey);
const jobPath = (id, tail) => '/jobs/' + encodeURIComponent(id) + (tail || '');

export function prepareUpdate() {
  const r = S.route;
  if (S.upd.job && S.upd.auditId !== r.auditId && isTerminal(S.upd.job.status)) S.upd = freshUpd();
  const V = S.upd;
  if (!V.job) {
    V.auditId = r.auditId;
    if (!V.findings || V.findingsFor !== r.auditId) {
      V.scope = r.findingId ? 'one' : 'all';
      V.selected = new Set(r.findingId ? [r.findingId] : []);
      loadFindings();
    }
  } else if (!isTerminal(V.job.status)) {
    // Polling may have given up while the panel was shut: pick the run up again.
    Object.assign(V, { error: '', pollError: '', pollFails: 0 });
    pollOnce();
  }
  render();
}

export async function loadFindings() {
  const V = S.upd;
  const auditId = S.route.auditId;
  if (!auditId) return;
  V.loadingFindings = true; V.error = '';
  render();
  try {
    const d = await api('/audits/' + encodeURIComponent(auditId) + '/findings');
    V.findings = (Array.isArray(d) ? d : d.findings || []).map((f) => ({ id: f._id || f.id, title: f.title || '(untitled finding)' }));
    V.findingsFor = auditId;
  } catch (e) {
    V.error = e.message;
    V.findings = [];
  }
  V.loadingFindings = false;
  render();
}

function scopeIds() {
  const V = S.upd;
  const all = (V.findings || []).map((f) => f.id);
  if (V.scope === 'one') return S.route.findingId ? [S.route.findingId] : [];
  if (V.scope === 'selected') return all.filter((id) => V.selected.has(id));
  return all;
}

export function viewUpdate() {
  const V = S.upd;
  const body = h('div', { class: 'ai-stack' });
  const foot = [];
  if (llmMissing()) body.append(notConfigured());
  if (V.error) body.append(note('bad', V.error));
  if (V.pollError) body.append(note('bad', V.pollError));
  if (V.warn) body.append(note('warn', V.warn));
  if (V.note) body.append(note('ok', V.note));
  if (V.job) return { title: 'AI run', body, foot, announce: jobView(body, foot) };
  formView(body, foot);
  return { title: 'Update with AI', body, foot };
}

function scopeSection(r) {
  const V = S.upd;
  const scopes = [];
  if (r.findingId) scopes.push(['one', 'This finding', 'The finding you are editing now']);
  scopes.push(['selected', 'Selected findings', 'Pick any findings from this audit']);
  scopes.push(['all', 'All findings in this audit', V.findings ? V.findings.length + ' findings' : '']);
  return section('Scope', h('div', { class: 'ai-choices' }, scopes.map(([v, l, hint]) =>
    choice('radio', 'scope', l, V.scope === v, () => { V.scope = v; render(); }, 'scope-' + v, hint))));
}

const MODES = [
  ['improve', 'Improve existing text', 'Rewrite and expand what is there'],
  ['fill_missing', 'Only fill empty fields', 'Never touch text you already wrote'],
];

function formView(body, foot) {
  const V = S.upd;
  const r = S.route;
  body.append(scopeSection(r));
  if (V.scope === 'selected') body.append(findingPicker());
  if (V.scope === 'one' && V.findings) {
    const f = V.findings.find((x) => x.id === r.findingId);
    body.append(h('p', { class: 'ai-quiet' }, f ? f.title : 'Finding ' + shortId(r.findingId)));
  }
  body.append(section('Fields to update', h('div', { class: 'ai-grid' }, FIELDS.map(([k, l]) =>
    choice('checkbox', 'f-' + k, l, V.fields.has(k), (e) => { e.target.checked ? V.fields.add(k) : V.fields.delete(k); render(); }, 'field-' + k)))));
  body.append(section('Mode', h('div', { class: 'ai-choices' }, MODES.map(([v, l, hint]) =>
    choice('radio', 'mode', l, V.mode === v, () => { V.mode = v; render(); }, 'mode-' + v, hint)))));
  body.append(section('Review',
    toggle('Apply automatically', 'Off by default: you review each change before it is written to PwnDoc.', V.auto,
      (e) => { V.auto = e.target.checked; render(); }, 'auto')));
  const ids = scopeIds();
  const ready = !V.loadingFindings && ids.length > 0 && V.fields.size > 0 && !V.starting && !llmMissing();
  foot.push(h('span', { class: 'ai-foot-info' }, V.loadingFindings ? 'Loading findings…' : plural(ids.length, 'finding') + ' · ' + V.fields.size + ' fields'));
  foot.push(btn('ai-primary', V.starting ? 'Starting…' : 'Start', startJob, { disabled: ready ? null : true, 'data-fk': 'start' }));
}

function findingPicker() {
  const V = S.upd;
  if (V.loadingFindings || !V.findings) return h('p', { class: 'ai-quiet' }, 'Loading findings…');
  if (!V.findings.length) return h('p', { class: 'ai-quiet' }, 'This audit has no findings yet.');
  const all = V.findings.map((f) => f.id);
  return h('div', { class: 'ai-picker' },
    h('div', { class: 'ai-picker-bar' },
      btn('ai-link', 'Select all', () => { V.selected = new Set(all); render(); }),
      btn('ai-link', 'Select none', () => { V.selected = new Set(); render(); })),
    h('div', { class: 'ai-list', role: 'group', 'aria-label': 'Findings' }, V.findings.map((f) =>
      choice('checkbox', 'sel-' + f.id, f.title, V.selected.has(f.id),
        (e) => { e.target.checked ? V.selected.add(f.id) : V.selected.delete(f.id); render(); }, 'sel-' + f.id))));
}

/** Start a run for the audit and scope the form was filled in for (not wherever the user is later). */
export async function startJob() {
  const V = S.upd;
  const auditId = S.route.auditId;
  const body = {
    audit_id: auditId, finding_ids: scopeIds(), fields: Array.from(V.fields), mode: V.mode, auto_apply: V.auto,
  };
  Object.assign(V, { starting: true, error: '', note: '', warn: '' });
  render();
  try {
    const resp = await api('/jobs', { method: 'POST', body });
    const id = resp.id || resp.job_id;
    const job = resp.items || resp.findings || resp.results ? normJob(resp) : normJob({ id, status: 'running' });
    storeJob({ id: job.id, auditId });
    if (S.upd !== V) return;  // the state was reset while posting: the run stays recoverable via storage
    Object.assign(V, {
      job, auditId, seen: new Set(), accept: new Set(), expanded: new Set(), sig: '', pollFails: 0, appliedAny: false,
    });
    absorb(job);
    ui.renderDock();
    pollOnce();
  } catch (e) {
    V.error = e.message;
  }
  V.starting = false;
  render();
}

/* ---------- job polling and results ---------- */
function absorb(job) {
  const V = S.upd;
  for (const it of job.items) {
    if (it.status === 'applied') V.appliedAny = true;
    if (appliable(it) && !V.seen.has(it.id)) { V.seen.add(it.id); V.accept.add(it.id); }
  }
}

function pollFailed(V, e) {
  const verdict = pollFailure(V, e);
  V.pollFails = verdict.fails || 0;
  if (verdict.gone) { V.job = null; V.error = verdict.message; storeJob(null); ui.renderDock(); }
  else if (verdict.stop) V.pollError = verdict.message;
  if (verdict.stop) render();
  return verdict.stop;
}

/** One status poll. Resolves true when the job view was refreshed. */
async function pollOnce() {
  clearTimeout(timers.poll);
  const V = S.upd;
  const jobId = V.job && V.job.id;
  if (!jobId) return false;
  let fresh = false;
  try {
    const j = normJob(await api(jobPath(jobId), { timeoutMs: POLL_TIMEOUT_MS }));
    if (S.upd !== V || !V.job || V.job.id !== jobId) return false;
    const recovered = !!V.pollError;
    Object.assign(V, { pollFails: 0, pollError: '' });
    fresh = true;
    const sig = JSON.stringify(j);
    if (sig !== V.sig) { V.sig = sig; V.job = j; absorb(j); render(); ui.renderDock(); }
    else if (recovered) render();
  } catch (e) {
    if (S.upd !== V || pollFailed(V, e)) return false;
  }
  if (V.job && !isTerminal(V.job.status)) timers.poll = setTimeout(pollOnce, POLL_MS);
  else { storeJob(null); ui.renderDock(); }
  return fresh;
}

async function cancelJob() {
  const V = S.upd;
  V.error = '';
  try {
    await api(jobPath(V.job.id, '/cancel'), { method: 'POST' });
  } catch (e) {
    V.error = e.message;
    render();
  }
  pollOnce();
}

/** What an apply request really did, judged from the job as the gateway reports it afterwards. */
function applyOutcome(V, ids) {
  const byId = new Map(V.job.items.map((i) => [i.id, i]));
  const written = ids.filter((id) => (byId.get(id) || {}).status === 'applied').length;
  const skipped = V.job.skipped.length;
  if (written === ids.length && !skipped) return { note: plural(written, 'finding') + ' written to PwnDoc.' };
  const parts = [written + ' of ' + plural(ids.length, 'finding') + ' written to PwnDoc.'];
  if (skipped) parts.push(plural(skipped, 'finding') + ' had nothing to apply.');
  if (written < ids.length) parts.push('See the failed items below; failed items can be applied again.');
  return { warn: parts.join(' ') };
}

async function applyItems(ids) {
  const V = S.upd;
  if (!ids.length || V.applying) return;
  Object.assign(V, { applying: true, error: '', note: '', warn: '' });
  render();
  let sent = false;
  try {
    await api(jobPath(V.job.id, '/apply'), { method: 'POST', body: { finding_ids: ids } });
    sent = true;
  } catch (e) {
    V.error = e.message;
  }
  V.applying = false;
  V.sig = '';
  const refreshed = await pollOnce();
  if (S.upd !== V) return;
  if (sent && !refreshed) V.warn = 'The apply request was sent, but its result could not be confirmed. Reopen this panel to check.';
  else if (sent) Object.assign(V, applyOutcome(V, ids));
  render();
}

function resetRun() {
  clearTimeout(timers.poll);
  storeJob(null);
  const r = S.route;
  S.upd = freshUpd();
  S.upd.auditId = r.auditId;
  ui.renderDock();
  prepareUpdate();
}

function runLine(job, terminal, current) {
  const usage = job.usage ? (job.usage.input || 0) + (job.usage.output || 0) : 0;
  const base = current ? 'Working on: ' + current.title : terminal ? 'Run finished.' : 'Waiting for the model…';
  return base + (usage ? ' · ' + fmtTokens(usage) + ' tokens' : '');
}

function runFoot(V, apList, terminal, foot) {
  if (!terminal) foot.push(btn('ai-ghost', 'Cancel run', cancelJob, { 'data-fk': 'cancel' }));
  if (apList.length) {
    const chosen = apList.filter((i) => V.accept.has(i.id)).map((i) => i.id);
    foot.push(btn('ai-ghost', 'Apply selected (' + chosen.length + ')', () => applyItems(chosen),
      { disabled: chosen.length && !V.applying ? null : true, 'data-fk': 'apply-sel' }));
    foot.push(btn('ai-primary', V.applying ? 'Applying…' : 'Apply all (' + apList.length + ')',
      () => applyItems(apList.map((i) => i.id)), { disabled: V.applying ? true : null, 'data-fk': 'apply-all' }));
  }
  if (V.appliedAny) foot.push(btn(apList.length ? 'ai-ghost' : 'ai-primary', 'Reload page', () => location.reload(), { 'data-fk': 'reload' }));
  if (terminal && !apList.length) foot.push(btn('ai-ghost', 'New run', resetRun, { 'data-fk': 'new-run' }));
}

/** Fills the run view and returns the sentence the live region should say. */
function jobView(body, foot) {
  const V = S.upd;
  const job = V.job;
  const terminal = isTerminal(job.status);
  const finished = countFinished(job.items);
  const current = job.items.find((i) => i.status === 'running');
  if (V.auditId !== S.route.auditId) body.append(note('warn', 'This run belongs to a different audit.'));
  if (job.error) body.append(note('bad', job.error));
  const line = runLine(job, terminal, current);
  body.append(progressCard({
    heading: terminal ? (STATUS_LABEL[job.status] || 'Finished') : 'Refining findings',
    finished, total: job.items.length, line,
  }));
  const apList = job.items.filter(appliable);
  const primary = primaryModel(job.items);
  body.append(h('ul', { class: 'ai-items' }, job.items.map((it) => itemView(it, primary))));
  if (V.appliedAny) body.append(note('ok', h('strong', {}, 'Changes are saved in PwnDoc. '), 'Reload the page to see them in the editor.'));
  runFoot(V, apList, terminal, foot);
  return line + (job.items.length ? ' ' + finished + ' of ' + job.items.length + '.' : '');
}

function diffView(c) {
  return h('div', { class: 'ai-diff' },
    h('h4', {}, c.label),
    h('div', { class: 'ai-diff-cols' },
      h('div', { class: 'ai-before' }, h('span', {}, 'Before'), h('pre', {}, c.before || '(empty)')),
      h('div', { class: 'ai-after' }, h('span', {}, 'After'), h('pre', {}, c.after || '(empty)'))));
}

function itemView(it, primary) {
  const V = S.upd;
  const open = V.expanded.has(it.id);
  const head = h('div', { class: 'ai-item-head' },
    appliable(it) ? h('label', { class: 'ai-pick' },
      h('input', { type: 'checkbox', checked: V.accept.has(it.id), 'aria-label': 'Apply ' + it.title, 'data-fk': 'acc-' + it.id,
        onchange: (e) => { e.target.checked ? V.accept.add(it.id) : V.accept.delete(it.id); render(); } }),
      h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }, icon('check'))) : null,
    h('span', { class: 'ai-item-title' }, it.title),
    statusChip(it),
    it.changes.length ? h('button', {
      type: 'button', class: 'ai-expand' + (open ? ' ai-open' : ''), 'aria-expanded': String(open), 'data-fk': 'exp-' + it.id,
      'aria-label': (open ? 'Hide' : 'Show') + ' changes for ' + it.title,
      onclick: () => { open ? V.expanded.delete(it.id) : V.expanded.add(it.id); render(); },
    }, icon('chev')) : null);
  return h('li', { class: 'ai-item' }, head,
    it.error ? h('p', { class: 'ai-item-error' }, it.error) : null,
    itemMeta(it, primary),
    open ? h('div', { class: 'ai-diffs' }, it.changes.map(diffView)) : null);
}

/** Resume a run found in sessionStorage after a page load. */
export function resumeJob(saved) {
  S.upd.job = normJob({ id: saved.id, status: 'running' });
  S.upd.auditId = saved.auditId;
  pollOnce();
}
