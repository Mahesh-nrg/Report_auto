/* pwndoc-ai: the state helpers of the "Refine a DOCX" drawer. The state itself is S.docx (see
 * ai-state.js); it is replaced, never edited, and every change goes through patch() so the open
 * drawer redraws exactly what changed. */
import { defaultPicked } from './ai-docx-util.js';
import { S } from './ai-state.js';

/** Filled in by the open drawer; a closed drawer leaves it a no-op (state still updates). */
export const hooks = { redraw: () => {} };

export const cur = () => S.docx;
export const filePath = (id, tail) => '/docx/files/' + encodeURIComponent(id) + (tail || '');
export const freshChanges = () => ({ fileId: '', items: [], total: 0, loading: false, error: '' });
export const freshSetup = (outline) => ({ scope: 'all', picked: defaultPicked(outline), mode: 'light', terms: '' });

export function patch(fields) {
  S.docx = { ...S.docx, ...fields };
  hooks.redraw();
}

/** An answer is still wanted: no logout or reset since `gen`, and (when given) the same document is open. */
export const live = (gen, id) => S.docx.gen === gen && (id === undefined || S.docx.selectedId === id);

/** What the open document is doing: the live status wins over what the list last said. */
export function effectiveState(D) {
  if (D.starting) return 'running';
  if (D.status) return D.status.state;
  return D.detail ? D.detail.file.state : 'uploaded';
}

/** The fields that make "no document open" (also after the open one was deleted or vanished). */
export const clearedSelection = () => ({
  selectedId: null, detail: null, status: null, setup: null, showSetup: null, starting: false,
  pollFails: 0, pollError: '', changes: freshChanges(), dl: { busy: '', error: '' },
});
