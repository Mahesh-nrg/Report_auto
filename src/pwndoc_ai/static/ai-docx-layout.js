/* pwndoc-ai: the layout check of a refined document's clean copy. The state is S.docx.dl (see
 * ai-docx-store.js): `layout` is the check, `tidy` the "Tidy layout" switch (on unless false) and
 * `info` what the last download did. dl is replaced, never edited, and a different document
 * clears it, so an answer for another document or an older run is dropped. */
import { api } from './ai-api.js';
import { failedLayout, freshLayout, layoutAnnouncement, layoutBlock, readyLayout } from './ai-layout.js';
import { CHECK_TIMEOUT_MS, normCheck } from './ai-layout-util.js';
import { cur, filePath, live, patch } from './ai-docx-store.js';
import { isAdmin, ui } from './ai-state.js';

export const tidyOn = (D) => D.dl.tidy !== false;
const setDl = (fields) => patch({ dl: { ...cur().dl, ...fields } });

/** Check the clean copy of the open document; a failure is shown quietly and never blocks a download. */
export async function loadDocxLayout(id) {
  const gen = cur().gen;
  const mine = freshLayout();
  setDl({ layout: mine });
  let layout;
  try {
    const check = normCheck(await api(filePath(id, '/check?kind=clean'), { timeoutMs: CHECK_TIMEOUT_MS }));
    layout = check ? readyLayout(check) : failedLayout();
  } catch (e) {
    layout = failedLayout();
  }
  if (!live(gen, id) || cur().dl.layout !== mine) return;
  setDl({ layout });
  const spoken = layoutAnnouncement(layout, tidyOn(cur()), false);
  if (spoken) ui.announce(spoken);
}

/** The "Layout check" block, and below it what the last download did. */
export function docxLayoutBlock(D) {
  return layoutBlock(D.dl.layout, tidyOn(D), {
    onTidy: (checked) => setDl({ tidy: checked }),
    onRetry: () => loadDocxLayout(D.selectedId),
  }, isAdmin());
}
