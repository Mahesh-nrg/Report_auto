/* pwndoc-ai: the state helpers of the "Export & fonts" drawer. The state is S.exp (ai-state.js); it
 * is replaced, never edited, and every change goes through patch() so the open drawer redraws only
 * what changed (identity checks in ai-export.js). */
import { S } from './ai-state.js';

/** Filled in by the open drawer; a closed drawer leaves it a no-op (state still updates). */
export const hooks = { redraw: () => {} };

export const BASE = '/docx/export';
export const cur = () => S.exp;
export const filePath = (id, tail) => BASE + '/files/' + encodeURIComponent(id) + (tail || '');

export function patch(fields) {
  S.exp = { ...S.exp, ...fields };
  hooks.redraw();
}

/** An answer is still wanted: no logout or reset since `gen`, and (when given) the same file is open. */
export const live = (gen, id) => S.exp.gen === gen && (id === undefined || S.exp.selectedId === id);

/** What the open file is doing: the live status wins over what the list last said. */
export function effectiveState(E) {
  if (E.starting) return 'analysing';
  if (E.status) return E.status.state;
  const file = selectedFile(E);
  return file ? file.state : '';
}

export const selectedFile = (E) => (E.detail && E.detail.file) || (E.files || []).find((f) => f.id === E.selectedId) || null;

export const clearedSelection = () => ({
  selectedId: null, detail: null, detailError: '', status: null, starting: false, pollFails: 0, error: '',
  plan: null, planRev: 0, saving: false, saveError: '', focusSection: '', confirmId: '', dl: { busy: '', error: '', info: '' },
});
