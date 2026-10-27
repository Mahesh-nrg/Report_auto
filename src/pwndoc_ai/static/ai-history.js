/* pwndoc-ai: History drawer: past AI runs and restorable backups. */
import { api } from './ai-api.js';
import { btn, chip, h, note, plural, section } from './ai-dom.js';
import { S, freshHist, ui } from './ai-state.js';
import { KIND_LABEL, MAX_LISTED, STATUS_LABEL, fmtTime, fmtTokens, objects, shortId } from './ai-util.js';

const render = (focusKey) => ui.renderBody('history', focusKey);
const auditPath = (auditId, tail) => '/audits/' + encodeURIComponent(auditId) + tail;

export function prepareHistory() {
  // A restore result outlives the drawer (it carries the "reload the page" prompt), but only
  // for the audit it belongs to.
  if (S.hist.auditId !== S.route.auditId) S.hist = freshHist(S.route.auditId);
  Object.assign(S.hist, { loadError: '', confirm: '' });
  render();
  loadHistory();
}

async function loadHistory() {
  const H = S.hist;
  H.loading = true; H.loadError = '';
  render();
  try {
    const d = await api(auditPath(H.auditId, '/history'));
    H.runs = objects(d && d.runs);
    H.backups = objects(d && d.backups).filter((b) => typeof b.name === 'string');
  } catch (e) {
    H.loadError = e.message;
  }
  H.loading = false;
  render();
}

const normReasons = (list) => objects(list).map((x) => ({ id: String(x.id || ''), reason: String(x.reason || '') }));

function normRestore(r) {
  return {
    restored: Number.isInteger(r && r.restored) ? r.restored : 0,
    skipped: normReasons(r && r.skipped),
    notes: normReasons(r && r.notes),
    safety: r && typeof r.safety_backup === 'string' ? r.safety_backup : '',
  };
}

async function restoreBackup(name) {
  const H = S.hist;
  Object.assign(H, { restoring: name, confirm: '', error: '', result: null });
  render();
  try {
    H.result = normRestore(await api(auditPath(H.auditId, '/backups/' + encodeURIComponent(name) + '/restore'), { method: 'POST' }));
  } catch (e) {
    H.error = e.status === 401 ? 'Your PwnDoc session expired. Sign in again, then retry the restore.' : e.message;
  }
  H.restoring = '';
  render();
  loadHistory();  // the safety backup of the previous state is now listed too
}

const RUN_TONE = { done: 'ok', error: 'bad', failed: 'bad', interrupted: 'warn', running: 'accent' };

function runView(r) {
  const status = String(r.status || 'done').toLowerCase();
  const models = (Array.isArray(r.models) && r.models.length ? r.models : [r.model]).filter((m) => typeof m === 'string' && m);
  const bits = [
    r.user ? 'By ' + r.user : '',
    Number.isInteger(r.applied) ? r.applied + ' applied' : '',
    Number.isInteger(r.failed) && r.failed ? r.failed + ' failed' : '',
    models.length ? 'Model: ' + models.join(', ') : '',
    Number.isInteger(r.tokens) && r.tokens ? fmtTokens(r.tokens) + ' tokens' : '',
    r.report ? 'Report built' : '',
  ].filter(Boolean);
  return h('li', { class: 'ai-item' },
    h('div', { class: 'ai-item-head' },
      h('span', { class: 'ai-item-title' }, (KIND_LABEL[r.kind] || 'AI run') + ' · ' + fmtTime(r.started)),
      chip(RUN_TONE[status] || 'mute', STATUS_LABEL[status] || status)),
    bits.length ? h('p', { class: 'ai-item-meta' }, bits.join(' · ')) : null,
    r.error ? h('p', { class: 'ai-item-error' }, String(r.error)) : null);
}

function restoreConfirm(name) {
  const H = S.hist;
  return note('warn',
    h('p', { class: 'ai-confirm-text' }, 'Replace the current text of these findings with this backup? The current state is saved as a new backup first, so a restore can be undone.'),
    h('div', { class: 'ai-row' },
      btn('ai-primary ai-sm', 'Yes, restore', () => restoreBackup(name), { 'data-fk': 'restore-yes' }),
      btn('ai-ghost ai-sm', 'Cancel', () => { H.confirm = ''; render('restore-' + name); }, { 'data-fk': 'restore-cancel' })));
}

function backupView(b) {
  const H = S.hist;
  const confirming = H.confirm === b.name;
  const count = Number.isInteger(b.findings) ? plural(b.findings, 'finding') : '';
  const row = h('div', { class: 'ai-item-head' },
    h('span', { class: 'ai-item-title' }, fmtTime(b.created), count ? h('small', { class: 'ai-sub' }, count) : null),
    confirming ? null : btn('ai-ghost ai-sm', 'Restore', () => { H.confirm = b.name; render('restore-yes'); },
      { disabled: H.restoring ? true : null, 'data-fk': 'restore-' + b.name, 'aria-label': 'Restore the backup from ' + fmtTime(b.created) }));
  return h('li', { class: 'ai-item' }, row, confirming ? restoreConfirm(b.name) : null,
    H.restoring === b.name ? h('p', { class: 'ai-quiet' }, 'Restoring…') : null);
}

const reasonList = (items) => h('ul', { class: 'ai-plain-list' },
  items.slice(0, MAX_LISTED).map((x) => h('li', {}, (x.id ? shortId(x.id) + ': ' : '') + x.reason)));

function restoreResult(r) {
  const out = [note('ok', h('strong', {}, plural(r.restored, 'finding') + ' restored. '), 'Reload the page to see the restored text.',
    r.safety ? h('small', { class: 'ai-block' }, 'The state before the restore was saved as ' + r.safety + '.') : null)];
  if (r.notes.length) {
    // restored, but PwnDoc keeps a value that the backup left blank (it ignores blank values)
    out.push(note('warn', h('strong', {}, plural(r.notes.length, 'finding') + ' could not be fully reverted'), reasonList(r.notes)));
  }
  if (r.skipped.length) out.push(note('warn', h('strong', {}, plural(r.skipped.length, 'finding') + ' skipped'), reasonList(r.skipped)));
  return out;
}

function listings(H) {
  return [
    section('Runs', H.runs.length ? h('ul', { class: 'ai-items' }, H.runs.map(runView))
      : h('p', { class: 'ai-quiet' }, 'No AI runs recorded for this audit yet.')),
    section('Backups', h('p', { class: 'ai-quiet' }, 'Taken before AI changes are saved to PwnDoc. Restoring puts the old text back.'),
      H.backups.length ? h('ul', { class: 'ai-items' }, H.backups.map(backupView))
        : h('p', { class: 'ai-quiet' }, 'No backups for this audit yet.')),
  ];
}

export function viewHistory() {
  const H = S.hist;
  const body = h('div', { class: 'ai-stack' });
  if (H.error) body.append(note('bad', H.error));
  if (H.result) body.append(...restoreResult(H.result));
  if (H.loadError) body.append(note('bad', H.loadError));
  else if (H.loading && !H.runs.length && !H.backups.length) body.append(h('p', { class: 'ai-quiet' }, 'Loading history…'));
  else body.append(...listings(H));
  const foot = [btn('ai-ghost', H.loading ? 'Refreshing…' : 'Refresh', loadHistory, { disabled: H.loading ? true : null, 'data-fk': 'history-refresh' })];
  if (H.result && H.result.restored) foot.push(btn('ai-primary', 'Reload page', () => location.reload(), { 'data-fk': 'history-reload' }));
  return { title: 'History', body, foot };
}
