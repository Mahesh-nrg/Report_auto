/* pwndoc-ai: "My documents": the uploaded reports of the signed-in user, their state, the
 * downloads of a refined one and the confirmed delete. */
import { btn, chip, focusKey, h, icon, note, plural } from './ai-dom.js';
import { copyButtons } from './ai-docx-download.js';
import { deleteFile } from './ai-docx-files.js';
import { selectFile } from './ai-docx-run.js';
import { patch } from './ai-docx-store.js';
import { fmtAge } from './ai-docx-util.js';
import { fmtBytes } from './ai-rag-files.js';
import { root } from './ai-shell.js';

const STATE_VIEW = {
  uploaded: ['mute', 'Uploaded'], running: ['accent', 'Running'], done: ['ok', 'Done'],
  error: ['bad', 'Failed'], cancelled: ['warn', 'Cancelled'], damaged: ['bad', 'Damaged'],
};
const ALMOST_FULL = 0.9;
const EMPTY_TEXT = 'No documents yet. Upload a Word report above to get started.';

function stateChip(state) {
  const [tone, label] = STATE_VIEW[state];
  return chip(tone, state === 'running' ? h('i', { class: 'ai-pulse' }) : null, label);
}

function quotaView(q) {
  if (!q) return [];
  const out = [h('p', { class: 'ai-quiet' }, fmtBytes(q.usedBytes) + ' of ' + fmtBytes(q.maxBytes) + ' used · ' + q.files + ' of ' + q.maxFiles + ' files')];
  if (q.maxFiles && q.files >= q.maxFiles) {
    out.push(note('warn', 'You have reached the limit of ' + q.maxFiles + ' files. Delete a document to upload another.'));
  } else if (q.maxBytes && q.usedBytes / q.maxBytes >= ALMOST_FULL) {
    out.push(note('warn', 'Your storage is almost full. Delete documents you no longer need.'));
  }
  return out;
}

function ask(id) {
  patch({ confirmId: id });
  focusKey(root, 'del-no');  // the safe answer first
}

function confirmView(f) {
  return note('warn',
    h('p', { class: 'ai-confirm-text' }, 'Delete ' + f.name + '? This removes the original and the refined copies (clean, tracked changes and PDF) from the gateway. It cannot be undone.'),
    h('div', { class: 'ai-row' },
      btn('ai-primary ai-sm', 'Delete', () => deleteFile(f.id), { 'data-fk': 'del-yes' }),
      btn('ai-ghost ai-sm', 'Keep', () => { patch({ confirmId: '' }); focusKey(root, 'del-' + f.id); }, { 'data-fk': 'del-no' })));
}

function actions(D, f) {
  const trash = h('button', {
    type: 'button', class: 'ai-x ai-x-sm', 'aria-label': 'Delete ' + f.name, 'data-fk': 'del-' + f.id,
    title: f.state === 'running' ? 'Cancel the run first' : null,
    disabled: f.state === 'running' || D.deletingId ? true : null, onclick: () => ask(f.id),
  }, icon('trash'));
  const damaged = f.state === 'damaged';  // its metadata is unreadable: only deleting it makes sense
  return h('div', { class: 'ai-row ai-doc-actions' },
    damaged ? null : btn('ai-ghost ai-sm', 'Open', () => selectFile(f.id), { 'data-fk': 'open-' + f.id, 'aria-label': 'Open ' + f.name }),
    f.state === 'done' ? copyButtons(D, f, true) : null, trash);
}

function row(D, f) {
  const selected = f.id === D.selectedId;
  const meta = [fmtBytes(f.size), f.created ? fmtAge(f.created, Date.now()) : '', f.state === 'done' ? plural(f.changed, 'change') : ''].filter(Boolean).join(' · ');
  return h('li', { class: 'ai-doc-item' + (selected ? ' ai-selected' : ''), 'aria-current': selected ? 'true' : null },
    h('div', { class: 'ai-doc-head' }, h('span', { class: 'ai-doc-name' }, f.name, h('small', {}, meta)), stateChip(f.state)),
    D.confirmId === f.id ? confirmView(f) : actions(D, f));
}

/** The quota line and the list (or its empty / loading text). */
export function listView(D) {
  const files = D.files;
  const body = files === null ? h('p', { class: 'ai-quiet' }, 'Loading your documents…')
    : files.length ? h('ul', { class: 'ai-docs ai-doc-list', 'aria-label': 'My documents' }, files.map((f) => row(D, f)))
      : h('p', { class: 'ai-quiet' }, EMPTY_TEXT);
  return [...quotaView(D.quota), body];
}

export const listKeys = (D) => [D.files, D.quota, D.confirmId, D.deletingId, D.selectedId, D.dl];
