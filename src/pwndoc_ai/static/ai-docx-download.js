/* pwndoc-ai: downloading the original and the refined copies of a document (Word, tracked
 * changes, PDF), fetched to a blob with the CSRF header like the audit report download. */
import { fetchApi } from './ai-api.js';
import { btn, note } from './ai-dom.js';
import { tidyOn } from './ai-docx-layout.js';
import { downloadSummary, tidyParam } from './ai-layout-util.js';
import { downloadFailureText } from './ai-docx-util.js';
import { cur, filePath, live, patch } from './ai-docx-store.js';
import { dispositionName, failureText, saveBlob } from './ai-download.js';
import { ui } from './ai-state.js';
import { DOWNLOAD_TIMEOUT_MS } from './ai-util.js';

/** The four files of a refined document. PDF is made from the clean copy. */
export const COPIES = [
  { fk: 'clean-docx', kind: 'clean', format: 'docx', label: 'Word (clean)' },
  { fk: 'tracked-docx', kind: 'tracked', format: 'docx', label: 'Word (tracked changes)' },
  { fk: 'clean-pdf', kind: 'clean', format: 'pdf', label: 'PDF' },
  { fk: 'original-docx', kind: 'original', format: 'docx', label: 'Original' },
];

export async function downloadCopy(id, copy) {
  const D = cur();
  if (D.dl.busy) return;
  const gen = D.gen;
  const tidy = copy.kind !== 'clean' || tidyOn(D);  // the original and the tracked copy are never tidied
  patch({ dl: { ...D.dl, busy: id + '|' + copy.fk, error: '', info: '' } });
  let error = '';
  let info = '';
  try {
    const query = ['kind=' + copy.kind, 'format=' + copy.format, tidyParam(tidy)].filter(Boolean).join('&');
    const res = await fetchApi(filePath(id, '/download?' + query), {}, DOWNLOAD_TIMEOUT_MS);
    if (res.ok) {
      saveBlob(await res.blob(), dispositionName(res, 'document.' + copy.format));
      if (copy.kind === 'clean') info = downloadSummary((name) => res.headers.get(name), tidy);
    } else error = downloadFailureText(res.status, copy.format) || await failureText(res);
  } catch (e) {
    error = e.message || 'Download failed.';
  }
  if (!live(gen)) return;
  patch({ dl: { ...cur().dl, busy: '', error, info } });
  if (info) ui.announce(info);
}

/** What the last download did (a note), or nothing. */
export const downloadInfo = (D) => (D.dl.info ? note('ok', D.dl.info) : null);

/** The four buttons. In a list row they carry the file name in their accessible name. */
export function copyButtons(D, file, inRow) {
  return COPIES.map((copy) => {
    const busy = D.dl.busy === file.id + '|' + copy.fk;
    return btn('ai-ghost' + (inRow ? ' ai-sm' : ''), busy ? 'Preparing…' : copy.label, () => downloadCopy(file.id, copy), {
      disabled: D.dl.busy ? true : null, 'data-fk': (inRow ? 'row-' + file.id + '-' : 'card-') + copy.fk,
      'aria-label': inRow ? 'Download ' + file.name + ' as ' + copy.label : null,
    });
  });
}
