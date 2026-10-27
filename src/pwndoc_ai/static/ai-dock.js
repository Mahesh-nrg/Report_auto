/* pwndoc-ai: the dock (floating buttons) and the signed-in status it depends on. */
import { api } from './ai-api.js';
import { h, icon } from './ai-dom.js';
import { syncImportUser } from './ai-import-state.js';
import { dockMount } from './ai-shell.js';
import { S, isAdmin, ui } from './ai-state.js';
import { countFinished, isTerminal } from './ai-util.js';

const STATUS_RETRY_MS = 5000;
const MAX_STATUS_RETRIES = 6;
let statusRetries = 0;
let statusTimer = null;

/* One row per audit button. `run` picks the running job (if any) whose progress replaces the label. */
const AUDIT_BUTTONS = [
  { kind: 'update', fk: 'dock-update', glyph: 'spark', label: 'Update with AI', busy: 'AI working', run: () => S.upd.job, cls: '' },
  { kind: 'finish', fk: 'dock-finish', glyph: 'doc', label: 'Finish report', busy: 'Finishing', run: () => S.fin.job, cls: ' ai-dock-finish' },
  { kind: 'download', fk: 'dock-download', glyph: 'download', label: 'Download report', cls: ' ai-dock-finish' },
  { kind: 'history', fk: 'dock-history', glyph: 'history', label: 'History', cls: ' ai-dock-finish' },
];

/* Not tied to an audit: for any signed-in user, on every page. */
const DOCX_BUTTON = { kind: 'docx', fk: 'dock-docx', glyph: 'doc', label: 'Refine a DOCX', cls: ' ai-dock-finish' };
const EXPORT_BUTTON = { kind: 'export', fk: 'dock-export', glyph: 'type', label: 'Export & fonts', cls: ' ai-dock-finish' };
const IMPORT_BUTTON = { kind: 'import', fk: 'dock-import', glyph: 'upload', label: 'Import a DOCX', cls: ' ai-dock-finish' };

function auditButton(spec) {
  const job = spec.run ? spec.run() : null;
  const busy = !!job && !isTerminal(job.status);
  const text = busy ? spec.busy + ' ' + countFinished(job.items) + '/' + job.items.length : spec.label;
  // No aria-label: the visible text is the accessible name, so the two can never disagree.
  return h('button', {
    type: 'button', class: 'ai-dock-main' + spec.cls + (busy ? ' ai-busy' : ''), 'data-fk': spec.fk,
    onclick: () => ui.openDrawer(spec.kind),
  }, icon(spec.glyph), h('span', { class: 'ai-dock-label' }, text));
}

function settingsButton(wide) {
  return h('button', {
    type: 'button', class: 'ai-dock-gear' + (wide ? ' ai-dock-wide' : ''), 'data-fk': 'dock-settings',
    onclick: () => ui.openDrawer('settings'), 'aria-label': 'AI Settings', title: 'AI Settings',
  }, icon('sliders'), wide ? h('span', { class: 'ai-dock-label' }, 'AI Settings') : null);
}

export function renderDock() {
  const st = S.status;
  const r = S.route;
  syncImportUser(r.kind === 'login' ? null : st);  // a signed-out or other user drops the import state
  if (!st || !st.authenticated || r.kind === 'login') { dockMount.replaceChildren(); return; }
  const kids = [];
  if (r.kind === 'audit') kids.push(...AUDIT_BUTTONS.map(auditButton));
  kids.push(auditButton(DOCX_BUTTON));
  if (st.docx_import) kids.push(auditButton(IMPORT_BUTTON));
  if (st.export_tab !== undefined) kids.push(auditButton(EXPORT_BUTTON));  // false: the drawer says how to install it
  if (isAdmin()) kids.push(settingsButton(!!r.settings));
  dockMount.replaceChildren(kids.length ? h('div', { class: 'ai-dock', role: 'group', 'aria-label': 'AI assistant' }, kids) : null);
}

/**
 * Ask the gateway who is signed in. A transient failure (gateway restarting, a 502) must not look
 * like a logout: the last known status is kept and the request is retried a few times.
 */
export async function refreshStatus() {
  clearTimeout(statusTimer);
  try {
    S.status = await api('/status');
    statusRetries = 0;
  } catch (e) {
    if (e.status === 401 || !S.status) S.status = { authenticated: false, error: e.message };
    if (e.status !== 401 && statusRetries++ < MAX_STATUS_RETRIES) {
      statusTimer = setTimeout(refreshStatus, STATUS_RETRY_MS);
    }
  }
  renderDock();
}

ui.renderDock = renderDock;
