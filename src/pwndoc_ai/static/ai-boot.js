/* pwndoc-ai: entry point. Registers the drawers, starts routing, resumes runs after a reload. */
import { refreshStatus } from './ai-dock.js';
import { disposeDocx, prepareDocx, resumeDocx, viewDocx } from './ai-docx.js';
import { prepareDownload, viewDownload } from './ai-download.js';
import { disposeExport, prepareExport, resumeExport, viewExport } from './ai-export.js';
import { prepareFinish, resumeFinish, viewFinish } from './ai-finish.js';
import { prepareHistory, viewHistory } from './ai-history.js';
import { disposeImport, importGuard, prepareImport, viewImport } from './ai-import.js';
import { host, loadStyles, registerView, syncTheme } from './ai-shell.js';
import { parseRoute, watchRoutes } from './ai-route.js';
import { disposeSettings, prepareSettings, settingsGuard, viewSettings } from './ai-settings.js';
import { S, readStored } from './ai-state.js';
import { prepareUpdate, resumeJob, viewUpdate } from './ai-update.js';
import { DOCX_KEY, EXPORT_KEY, FIN_KEY, JOB_KEY } from './ai-util.js';

registerView('update', { prepare: prepareUpdate, render: viewUpdate });
registerView('finish', { prepare: prepareFinish, render: viewFinish });
registerView('download', { prepare: prepareDownload, render: viewDownload });
registerView('history', { prepare: prepareHistory, render: viewHistory });
registerView('docx', { prepare: prepareDocx, render: viewDocx, dispose: disposeDocx });
registerView('import', { prepare: prepareImport, render: viewImport, guard: importGuard, dispose: disposeImport });
registerView('export', { prepare: prepareExport, render: viewExport, dispose: disposeExport });
registerView('settings', { prepare: prepareSettings, render: viewSettings, guard: settingsGuard, dispose: disposeSettings });

function watchTheme() {
  syncTheme();
  new MutationObserver(syncTheme).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncTheme);
}

/** Runs that were going when the page was reloaded are picked up again. */
function resumeRuns() {
  const signedIn = !!(S.status && S.status.authenticated);
  const job = readStored(JOB_KEY);
  if (job && job.id && signedIn && job.auditId === S.route.auditId) resumeJob(job);
  const fin = readStored(FIN_KEY);
  if (fin && fin.id && signedIn) resumeFinish(fin);  // owner-scoped on the server
  const docx = readStored(DOCX_KEY);
  if (docx && docx.id && signedIn) resumeDocx(docx);  // followed again when the drawer is opened
  const exp = readStored(EXPORT_KEY);
  if (exp && exp.id && signedIn) resumeExport(exp);
}

async function boot() {
  document.body.append(host);
  watchTheme();
  await loadStyles();
  host.style.visibility = 'visible';
  S.route = parseRoute();
  if (S.route.kind === 'audit') S.upd.scope = S.route.findingId ? 'one' : 'all';
  watchRoutes();
  await refreshStatus();
  resumeRuns();
}

if (document.body) boot();
else document.addEventListener('DOMContentLoaded', boot);

