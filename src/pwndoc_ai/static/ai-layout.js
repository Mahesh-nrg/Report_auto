/* pwndoc-ai: the "Layout check" block of the Download report drawer and of the DOCX result view:
 * what the gateway's check route found, the "Tidy layout" switch and the font warning. The state
 * lives in S (so a logout clears it); this file only draws it. All text is drawn as text nodes. */
import { btn, chip, h, note, section, toggle } from './ai-dom.js';
import { fontWarning, layoutHeadline, layoutPhrases, tidyNote } from './ai-layout-util.js';

export const freshLayout = () => ({ status: 'loading', data: null, error: '' });
export const readyLayout = (data) => ({ status: 'ready', data, error: '' });
export const failedLayout = () => ({ status: 'error', data: null, error: 'The layout check is unavailable right now. You can still download.' });

const SWITCH_LABEL = 'Tidy layout (recommended)';
const SWITCH_HINT = 'Fixes blank pages, stray empty paragraphs and empty sections in the download. Your text, images and tables are never changed.';
const LOADING_TEXT = 'Checking the layout…';
const UNINSTALLED = 'The layout check is not installed on this gateway, so downloads are not tidied.';

function findings(check, tidyOn) {
  const phrases = layoutPhrases(check);
  if (!phrases.length) return h('p', { class: 'ai-row' }, chip('ok', layoutHeadline(check, tidyOn)));
  return note('warn', h('strong', {}, 'Found: '), h('ul', { class: 'ai-layout-list' }, phrases.map((p) => h('li', {}, p))),
    h('p', { class: 'ai-item-meta' }, tidyNote(tidyOn)));
}

function body(layout, tidyOn, onRetry, admin) {
  if (!layout || layout.status === 'loading') return [h('p', { class: 'ai-quiet' }, LOADING_TEXT)];
  if (layout.status === 'error') {
    return [h('p', { class: 'ai-quiet' }, layout.error),
      btn('ai-ghost ai-sm', 'Check again', onRetry, { 'data-fk': 'layout-retry' })];
  }
  const check = layout.data;
  if (!check.available) return [h('p', { class: 'ai-quiet' }, UNINSTALLED)];
  if (check.note) return [h('p', { class: 'ai-quiet' }, check.note)];
  const warning = fontWarning(check, admin);
  return [findings(check, tidyOn), warning ? note('warn', warning) : null];
}

/**
 * The block. `handlers.onTidy(checked)` runs when the switch is flipped, `handlers.onRetry()`
 * when the person asks to check again after a failure.
 */
export function layoutBlock(layout, tidyOn, handlers, admin) {
  return section('Layout check', h('div', { class: 'ai-stack-tight', 'data-layout': layout ? layout.status : 'idle' },
    body(layout, tidyOn, handlers.onRetry, admin),
    toggle(SWITCH_LABEL, SWITCH_HINT, tidyOn, (e) => handlers.onTidy(e.target.checked), 'layout-tidy')));
}

/**
 * What to say aloud when a check finished (the drawer's persistent live region speaks it). The
 * Download drawer's shell already speaks the warning notes of a render (`notesSpoken`), so there
 * only "looks good" needs saying; the DOCX drawer redraws without that, so it says everything.
 */
export function layoutAnnouncement(layout, tidyOn, notesSpoken) {
  if (!layout || layout.status !== 'ready' || !layout.data.available || layout.data.note) return '';
  const phrases = layoutPhrases(layout.data);
  if (phrases.length && notesSpoken) return '';
  return 'Layout check: ' + layoutHeadline(layout.data, tidyOn) + (phrases.length ? '' : '.');
}
