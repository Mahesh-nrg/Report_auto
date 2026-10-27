/* pwndoc-ai: widgets shared by the Update, Finish and Download drawers. */
import { btn, chip, h, note } from './ai-dom.js';
import { S, isAdmin, ui } from './ai-state.js';
import { MAX_SOURCES, STATUS_LABEL } from './ai-util.js';

export function notConfigured() {
  return note('warn', h('strong', {}, 'The LLM is not configured yet. '),
    isAdmin()
      ? [h('span', {}, 'Add a provider, API key and model to continue. '),
        btn('ai-link', 'Open AI Settings', () => ui.openDrawer('settings'))]
      : 'Ask a PwnDoc administrator to set it up in AI Settings.');
}

export const llmMissing = () => !!(S.status && S.status.llm_configured === false);

// The model that answered most findings of a job is treated as the primary one unless the
// settings (admins) name it; only findings answered by another model show their model.
export function primaryModel(items) {
  const named = (S.set.data && S.set.data.llm_model) || (S.status && S.status.llm_model);
  if (named) return String(named);
  const counts = new Map();
  for (const it of items) if (it.model) counts.set(it.model, (counts.get(it.model) || 0) + 1);
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0]?.[0] || '';
}

export function itemMeta(it, primary) {
  const out = [];
  if (it.model && it.model !== primary) out.push(h('p', { class: 'ai-item-meta' }, 'Model: ' + it.model));
  if (it.sources.length) {
    const more = it.sources.length - MAX_SOURCES;
    out.push(h('p', { class: 'ai-item-meta' },
      'Sources: ' + it.sources.slice(0, MAX_SOURCES).join(', ') + (more > 0 ? ' and ' + more + ' more' : '')));
  }
  return out;
}

const ITEM_TONE = { applied: 'ok', proposed: 'accent', failed: 'bad', error: 'bad', running: 'accent' };

/** The status chip of one job item. */
export const statusChip = (it) =>
  chip(ITEM_TONE[it.status] || 'mute', it.status === 'running' ? h('i', { class: 'ai-pulse' }) : null, STATUS_LABEL[it.status] || it.status);

/** Progress card shared by both runs: heading, "n of m", bar and one status line. */
export function progressCard({ heading, finished, total, line }) {
  const pct = total ? Math.round((finished / total) * 100) : 0;
  return h('div', { class: 'ai-progress' },
    h('div', { class: 'ai-progress-top' },
      h('strong', { class: 'ai-big' }, heading),
      h('span', { class: 'ai-quiet' }, total ? finished + ' of ' + total : 'Starting…')),
    h('div', {
      class: 'ai-bar', role: 'progressbar', 'aria-label': 'Run progress', 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': String(pct), 'aria-valuetext': total ? finished + ' of ' + total + ' findings' : 'Starting',
    }, h('i', { style: 'transform:scaleX(' + pct / 100 + ')' })),
    h('p', { class: 'ai-quiet' }, line));
}

