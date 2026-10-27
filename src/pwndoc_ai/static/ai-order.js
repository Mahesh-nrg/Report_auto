/* pwndoc-ai: "Findings order and verification" section (docs/FINDING_ORDER.md).
   Shows the report order (CVSS 3.1, Critical to Informational, numbered 001..), the severity
   derived from the score, the proposed changes and the verification, then lets the user fill
   empty fields with AI (reviewed in Update with AI) and apply the normalisation (backup first). */
import { api } from './ai-api.js';
import { btn, chip, h, note, plural, section } from './ai-dom.js';
import { S } from './ai-state.js';

const state = { auditId: '', data: null, busy: '', error: '', done: '' };
const path = (suffix) => '/audits/' + encodeURIComponent(S.route.auditId) + suffix;

function reset() {
  Object.assign(state, { auditId: S.route.auditId, data: null, busy: '', error: '', done: '' });
}

async function run(rerender, label, work) {
  Object.assign(state, { busy: label, error: '', done: '' });
  rerender();
  try { await work(); } catch (e) { state.error = e.message; }
  state.busy = '';
  rerender();
}

export const loadOrder = (rerender) => run(rerender, 'Checking…', async () => {
  state.data = await api(path('/findings/order'));
});

const applyOrder = (rerender) => run(rerender, 'Saving…', async () => {
  const result = await api(path('/normalize'), { method: 'POST', body: { digest: state.data.digest } });
  state.done = result.written
    ? 'Saved ' + plural(result.written, 'finding') + ' (backup ' + result.backup + ', Restore can undo it).'
    : 'Nothing to change.';
  state.data = await api(path('/findings/order'));
});

const fillWithAi = (rerender) => run(rerender, 'Starting…', async () => {
  await api(path('/normalize/fill'), { method: 'POST' });
  state.done = 'AI is filling the empty fields. Review and apply them in “Update with AI”, then check again.';
});

function row(f) {
  const tone = { Critical: 'bad', High: 'bad', Medium: 'warn', Low: 'ok', Informational: 'mute' }[f.severity] || 'mute';
  const label = f.mismatch ? f.severity + ' (stored: ' + f.severity_stored + ')' : f.severity;
  const score = f.score == null ? '-' : Number(f.score).toFixed(1);
  return h('li', {},
    h('strong', {}, f.seq + ' '), f.title || '(untitled)', ' ',
    chip(tone, label + ' ' + score),
    f.estimated ? chip('warn', 'estimated by AI/heuristic - review') : null,
    Object.keys(f.changes || {}).length ? chip('mute', plural(Object.keys(f.changes).length, 'change')) : null);
}

function summary(v) {
  const s = v.summary;
  return note(v.normalised ? 'ok' : 'warn',
    v.normalised ? 'Findings are normalised and in report order.' : 'Findings need normalising before the report.',
    ' ' + s.pass + ' passed, ' + s.fixed + ' to fix, ' + s.fail + ' failed, ' + s.warn + ' warnings.');
}

export function orderSection(rerender) {
  if (state.auditId !== S.route.auditId) reset();
  const kids = [];
  if (state.error) kids.push(note('bad', state.error));
  if (state.done) kids.push(note('ok', state.done));
  const d = state.data;
  if (d) {
    kids.push(summary(d.verification), h('ol', { class: 'ai-steps' }, d.findings.map(row)));
    if (d.normalise_before_download) kids.push(note('mute', 'Downloads normalise first (Settings), behind the automatic backup.'));
  }
  const busy = state.busy ? true : null;
  kids.push(h('div', { class: 'ai-row' },
    btn('', state.busy || (d ? 'Check again' : 'Check findings order'), () => loadOrder(rerender), { disabled: busy }),
    d && !d.verification.normalised ? btn('', 'Fill empty fields with AI', () => fillWithAi(rerender), { disabled: busy }) : null,
    d && !d.verification.normalised ? btn('ai-primary', 'Apply normalisation', () => applyOrder(rerender), { disabled: busy }) : null));
  return section('Findings order (CVSS 3.1)', kids);
}
