/* pwndoc-ai: pure helpers of the "Import a DOCX" drawer (no DOM, tested in node).
 *
 * The importer's rich text is allow-list HTML (p, br, ul, ol, li, strong, em, code, pre). The UI
 * never parses it with the browser: htmlTree() is a small tokenizer that keeps only those tags (no
 * attribute ever survives) and the view builds text nodes from the tree. The editable convention
 * turns that HTML into plain text for a textarea and back: a blank line separates paragraphs, a
 * single newline is a <br>, "- " lines are a bullet list, "1. " lines a numbered list (two spaces
 * of indent per level), **bold** and `code` keep their marks and ``` fences a code block. */

export const IMPORT_ID_RE = /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/;
export const FINDING_KEY_RE = /^f\d{3,4}$/;
export const IMAGE_KEY_RE = /^i\d{3,4}$/;
export const OBJECT_ID_RE = /^[0-9a-f]{24}$/;
export const SEVERITIES = ['Critical', 'High', 'Medium', 'Low', 'Informational'];
export const LEVEL_LABEL = { blocker: 'Required', warning: 'Check', info: 'Note' };
export const LEVEL_TONE = { blocker: 'bad', warning: 'warn', info: 'mute' };
export const PHASE_LABEL = { analyse: 'Reading the report', create: 'Creating in PwnDoc', undo: 'Undoing the import' };
export const STATE_LABEL = {
  analysing: 'Reading', ready: 'Ready to check', analysis_failed: 'Could not read', cancelled: 'Cancelled',
  creating: 'Creating', created: 'Created', create_failed: 'Create failed', undoing: 'Undoing',
  undone: 'Undone', undo_failed: 'Undo failed', interrupted: 'Interrupted',
};
export const STATE_TONE = {
  ready: 'accent', created: 'ok', undone: 'mute', analysis_failed: 'bad', create_failed: 'bad',
  undo_failed: 'bad', interrupted: 'warn', cancelled: 'warn',
};
const RUNNING = new Set(['analysing', 'creating', 'undoing']);
const FAILED = new Set(['analysis_failed', 'cancelled', 'create_failed', 'undo_failed', 'interrupted']);
const PHASES = new Set(['', 'analyse', 'create', 'undo']);
const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

export const isImportId = (v) => typeof v === 'string' && IMPORT_ID_RE.test(v);
export const isRunning = (state) => RUNNING.has(state);
const str = (v) => (typeof v === 'string' ? v : '');
const int = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);
const strs = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
const recs = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === 'object' && !Array.isArray(x)) : []);
export const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');

/* ---------- server answers (rendered as text only, still shape-checked) ---------- */

function normResult(raw) {
  if (!raw || typeof raw !== 'object' || !OBJECT_ID_RE.test(str(raw.audit_id))) return null;
  return Object.freeze({
    auditId: raw.audit_id, companyId: OBJECT_ID_RE.test(str(raw.company_id)) ? raw.company_id : '',
    companyCreated: raw.company_created === true, clientCreated: raw.client_created === true,
    findings: strs(raw.finding_ids).length, uploaded: int(raw.images_uploaded),
    reused: int(raw.images_reused), skipped: int(raw.images_skipped),
  });
}

function normUndo(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const items = recs(raw.items).map((i) => Object.freeze({ kind: str(i.kind), id: str(i.id), outcome: str(i.outcome) }));
  return Object.freeze({ items: Object.freeze(items) });
}

/** An ImportStatus answer, or null when it is not one. */
export function normStatus(raw) {
  if (!raw || typeof raw !== 'object' || !isImportId(raw.id) || !(raw.state in STATE_LABEL)) return null;
  return Object.freeze({
    id: raw.id, fileId: str(raw.file_id), fileName: str(raw.file_name) || 'report.docx', state: raw.state,
    phase: PHASES.has(raw.phase) ? raw.phase : '', total: int(raw.total), done: int(raw.done),
    current: str(raw.current), error: str(raw.error), started: str(raw.started), ended: str(raw.ended),
    revision: int(raw.revision), hasPreview: raw.has_preview === true,
    result: normResult(raw.result), undo: normUndo(raw.undo),
  });
}

export function normList(raw) {
  const imports = recs(raw && raw.imports).map(normStatus).filter(Boolean);
  return Object.freeze({ imports: Object.freeze(imports), limit: int(raw && raw.limit) });
}

function normFinding(f) {
  const poc = recs(f.poc).filter((b) => (b.kind === 'image' ? IMAGE_KEY_RE.test(str(b.image_key)) : b.kind === 'html'))
    .map((b) => Object.freeze(b.kind === 'image' ? { kind: 'image', image_key: b.image_key } : { kind: 'html', html: str(b.html) }));
  return Object.freeze({
    key: f.key, source_id: str(f.source_id), title: str(f.title), severity: SEVERITIES.includes(f.severity) ? f.severity : '',
    cvss_vector: str(f.cvss_vector), cvss_score: str(f.cvss_score), cwe: str(f.cwe), urls: strs(f.urls),
    parameter: str(f.parameter), location: str(f.location), description: str(f.description),
    observation: str(f.observation), poc: Object.freeze(poc), remediation: str(f.remediation),
    references: strs(f.references), include: f.include !== false, origin: str(f.origin),
    llm_fields: strs(f.llm_fields), flags: strs(f.flags),
  });
}

function normImage(i) {
  return Object.freeze({
    key: i.key, media_type: str(i.media_type), size: int(i.size), width: int(i.width), height: int(i.height),
    caption: str(i.caption), finding_key: str(i.finding_key), source_finding_key: str(i.source_finding_key),
    include: i.include === true, order: int(i.order), flags: strs(i.flags),
    usable: !!str(i.media_type) && !strs(i.flags).some((x) => x === 'unsupported' || x === 'too_large'),
  });
}

function normHeader(h) {
  const c = h && h.contact && typeof h.contact === 'object' ? h.contact : null;
  return Object.freeze({
    company: str(h && h.company), audit_name: str(h && h.audit_name), language: str(h && h.language),
    audit_type: str(h && h.audit_type), template_id: str(h && h.template_id), date_start: str(h && h.date_start),
    date_end: str(h && h.date_end), scope: strs(h && h.scope),
    contact: c && str(c.email) ? Object.freeze({ email: c.email, firstname: str(c.firstname), lastname: str(c.lastname) }) : null,
  });
}

function normOptions(o) {
  const choices = (v) => recs(v).map((c) => Object.freeze({ value: str(c.value), label: str(c.label) || str(c.value) })).filter((c) => c.value);
  const types = recs(o && o.audit_types).map((t) => Object.freeze({ name: str(t.name), locales: strs(t.locales) })).filter((t) => t.name);
  return Object.freeze({ languages: choices(o && o.languages), audit_types: types, templates: choices(o && o.templates) });
}

const normIssue = (i) => Object.freeze({
  code: str(i.code), level: i.level in LEVEL_LABEL ? i.level : 'info', message: str(i.message),
  finding_key: str(i.finding_key), image_key: str(i.image_key), fields: strs(i.fields),
});

/** key -> { seq, severity, score, note } from the server's verification (FINDING_ORDER.md). */
function normOrder(v) {
  const out = {};
  for (const e of recs(v && v.findings)) {
    if (!FINDING_KEY_RE.test(str(e.key))) continue;
    const score = typeof e.score === 'number' && Number.isFinite(e.score) ? e.score : null;
    out[e.key] = Object.freeze({ seq: str(e.seq), severity: str(e.severity), score, note: str(e.note) });
  }
  return Object.freeze(out);
}

/** The preview (ImportPlan JSON), or null when it is not one. Keys keep the server's names. */
export function normPlan(raw) {
  if (!raw || typeof raw !== 'object' || !isImportId(raw.import_id)) return null;
  const src = raw.source_header && typeof raw.source_header === 'object' ? raw.source_header : {};
  return Object.freeze({
    import_id: raw.import_id, revision: int(raw.revision), file_name: str(raw.file_name), header: normHeader(raw.header),
    candidates: strs(src.company_candidates), leftovers: strs(src.leftover_names),
    findings: Object.freeze(recs(raw.findings).filter((f) => FINDING_KEY_RE.test(str(f.key))).map(normFinding)),
    images: Object.freeze(recs(raw.images).filter((i) => IMAGE_KEY_RE.test(str(i.key))).map(normImage)),
    issues: Object.freeze(recs(raw.issues).map(normIssue)), stats: Object.freeze({ ...(raw.stats || {}) }),
    options: normOptions(raw.options), company_id: str(raw.company_id),
    duplicate_audit: raw.duplicate_audit === true, previous_import: str(raw.previous_import),
    order: normOrder(raw.verification),
  });
}

/** Which step the drawer shows (S.imp.step). */
export function stepOf(slice) {
  if (slice.uploading || slice.starting) return 'analysing';
  const st = slice.status;
  if (!st) return 'pick';
  if (st.state === 'analysing') return 'analysing';
  if (st.state === 'ready') return slice.confirm === 'create' ? 'confirm' : 'preview';
  if (st.state === 'creating' || st.state === 'created' || st.state === 'undoing' || st.state === 'undone') return st.state;
  return FAILED.has(st.state) ? 'failed' : 'pick';
}

/* ---------- allow-list HTML tokenizer ---------- */

const ALLOWED = new Set(['p', 'br', 'ul', 'ol', 'li', 'strong', 'em', 'code', 'pre']);
const DROPPED = new Set(['script', 'style', 'template', 'iframe', 'object', 'embed', 'svg', 'math', 'textarea', 'noscript', 'title', 'head', 'select', 'xmp']);
const TOKEN_RE = /<!--[\s\S]*?(?:-->|$)|<(\/?)([a-zA-Z][a-zA-Z0-9]*)\b(?:[^>"']|"[^"]*"|'[^']*')*>|<|[^<]+/g;
const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', '#39': "'" };

export function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-f]{1,6}|#\d{1,7}|[a-z]{2,6});/gi, (all, name) => {
    const low = name.toLowerCase();
    if (low[0] !== '#') return NAMED[low] !== undefined ? NAMED[low] : all;
    const code = low[1] === 'x' ? parseInt(low.slice(2), 16) : parseInt(low.slice(1), 10);
    const bad = !(code > 0 && code <= 0x10ffff) || (code >= 0xd800 && code <= 0xdfff);
    return bad ? '�' : String.fromCodePoint(code);
  });
}

function closeTag(stack, name) {
  const at = stack.map((n) => n.tag).lastIndexOf(name);
  if (at > 0) stack.length = at;
}

/** Allow-list HTML as a tree: nodes are strings or {tag, kids}. Every attribute is dropped. */
export function htmlTree(html) {
  const top = { tag: '', kids: [] };
  const stack = [top];
  let dropping = '';
  for (const m of String(html || '').matchAll(TOKEN_RE)) {
    const [token, slash, rawName] = m;
    const name = rawName ? rawName.toLowerCase() : '';
    if (dropping) { if (slash && name === dropping) dropping = ''; continue; }
    if (token.startsWith('<!--')) continue;
    if (!name) { stack[stack.length - 1].kids.push(token === '<' ? '<' : decodeEntities(token)); continue; }
    if (!slash && DROPPED.has(name)) { dropping = name; continue; }
    if (!ALLOWED.has(name)) continue;
    if (slash) { closeTag(stack, name); continue; }
    const node = { tag: name, kids: [] };
    stack[stack.length - 1].kids.push(node);
    if (name !== 'br' && !token.endsWith('/>')) stack.push(node);
  }
  return top.kids;
}

/* ---------- allow-list HTML -> editable text ---------- */

function inlineText(nodes) {
  return nodes.map((n) => {
    if (typeof n === 'string') return n.replace(/\s*\n\s*/g, ' ');
    if (n.tag === 'br') return '\n';
    const inner = inlineText(n.kids);
    if (n.tag === 'strong') return inner.trim() ? '**' + inner + '**' : inner;
    if (n.tag === 'code') return inner.trim() ? '`' + inner + '`' : inner;
    if (n.tag === 'ul' || n.tag === 'ol') return ' ' + listLines(n, 0).join(' ') + ' ';
    return n.tag === 'p' || n.tag === 'li' ? ' ' + inner + ' ' : inner;
  }).join('');
}

const tidy = (s) => s.split('\n').map((l) => l.replace(/[ \t ]+/g, ' ').trim()).join('\n').trim();

function listLines(list, depth) {
  const out = [];
  let index = 0;
  for (const li of list.kids) {
    if (typeof li === 'string' || li.tag !== 'li') continue;
    index += 1;
    const nested = li.kids.filter((k) => typeof k !== 'string' && (k.tag === 'ul' || k.tag === 'ol'));
    const text = tidy(inlineText(li.kids.filter((k) => !nested.includes(k)))).replace(/\n/g, ' ');
    out.push('  '.repeat(depth) + (list.tag === 'ol' ? index + '. ' : '- ') + text);
    for (const sub of nested) out.push(...listLines(sub, depth + 1));
  }
  return out;
}

const textOf = (nodes) => nodes.map((n) => (typeof n === 'string' ? n : n.tag === 'br' ? '\n' : textOf(n.kids))).join('');

function blockText(node) {
  if (node.tag === 'ul' || node.tag === 'ol') return listLines(node, 0).join('\n');
  if (node.tag === 'pre') return '```\n' + textOf(node.kids).replace(/^\n+|\n+$/g, '') + '\n```';
  return tidy(inlineText(node.kids));
}

/** Allow-list HTML as editable text (see the convention at the top of this file). */
export function htmlToEditable(html) {
  const blocks = [];
  let loose = [];
  const flush = () => { const t = tidy(inlineText(loose)); if (t) blocks.push(t); loose = []; };
  for (const node of htmlTree(html)) {
    if (typeof node !== 'string' && ['p', 'ul', 'ol', 'pre', 'li'].includes(node.tag)) {
      flush();
      const t = node.tag === 'li' ? blockText({ tag: 'ul', kids: [node] }) : blockText(node);
      if (t) blocks.push(t);
    } else loose.push(node);
  }
  flush();
  return blocks.join('\n\n');
}

/* ---------- editable text -> allow-list HTML ---------- */

export const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function inlineHtml(text) {
  return escapeHtml(text).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

const LIST_RE = /^( *)(?:[-*•]|(\d{1,4})[.)]) +(\S.*)$/;

function listHtml(items) {
  let html = '';
  const open = [];  // tags of the lists open per level
  for (const { level, tag, text } of items) {
    while (open.length > level + 1) html += '</li></' + open.pop() + '>';
    if (open.length === level + 1 && open[level] !== tag) html += '</li></' + open.pop() + '>';
    if (open.length === level + 1) html += '</li>';
    while (open.length < level + 1) { html += '<' + tag + '>'; open.push(tag); }
    html += '<li><p>' + inlineHtml(text) + '</p>';
  }
  while (open.length) html += '</li></' + open.pop() + '>';
  return html;
}

function listItems(lines) {
  let level = -1;
  return lines.map((line) => {
    const m = LIST_RE.exec(line);
    level = Math.min(Math.floor(m[1].length / 2), level + 1);
    return { level, tag: m[2] ? 'ol' : 'ul', text: m[3].trim() };
  });
}

function paragraphHtml(lines) {
  const parts = lines.map((l) => l.trim()).filter(Boolean).map(inlineHtml);
  return parts.length ? '<p>' + parts.join('<br>') + '</p>' : '';
}

/** Split text into runs: {kind: 'code'|'list'|'para', lines}. A blank line ends a run. */
function runs(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i += 1; continue; }
    if (/^\s*```/.test(lines[i])) {
      const end = lines.findIndex((l, j) => j > i && /^\s*```\s*$/.test(l));
      const stop = end < 0 ? lines.length : end;
      out.push({ kind: 'code', lines: lines.slice(i + 1, stop) });
      i = stop + 1;
      continue;
    }
    const kind = LIST_RE.test(lines[i]) ? 'list' : 'para';
    const start = i;
    while (i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]) && (LIST_RE.test(lines[i]) ? 'list' : 'para') === kind) i += 1;
    out.push({ kind, lines: lines.slice(start, i) });
  }
  return out;
}

/** Editable text as allow-list HTML; "" when nothing is left. All text is escaped. */
export function editableToHtml(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').replace(CONTROL_RE, '').split('\n');
  return runs(lines).map((run) => {
    if (run.kind === 'code') {
      const body = run.lines.join('\n').replace(/^\n+|\n+$/g, '');
      return body.trim() ? '<pre>' + escapeHtml(body) + '</pre>' : '';
    }
    return run.kind === 'list' ? listHtml(listItems(run.lines)) : paragraphHtml(run.lines);
  }).join('');
}

/* ---------- PoC blocks <-> editable text ---------- */

const MARKER_RE = /^\s*\[Screenshot (i\d{3,4})\]\s*$/;
export const marker = (key) => '[Screenshot ' + key + ']';

export function pocToEditable(blocks) {
  return (blocks || []).map((b) => (b.kind === 'image' ? marker(b.image_key) : htmlToEditable(b.html))).filter(Boolean).join('\n\n');
}

/** Editable PoC text as blocks. A marker line of a known screenshot becomes an image block (once). */
export function editableToPoc(text, imageKeys) {
  const known = new Set(imageKeys || []);
  const seen = new Set();
  const blocks = [];
  let buffer = [];
  const flush = () => {
    const html = editableToHtml(buffer.join('\n'));
    if (html) blocks.push({ kind: 'html', html });
    buffer = [];
  };
  for (const line of String(text || '').replace(/\r\n?/g, '\n').split('\n')) {
    const m = MARKER_RE.exec(line);
    if (m && known.has(m[1])) {
      flush();
      if (!seen.has(m[1])) blocks.push({ kind: 'image', image_key: m[1] });
      seen.add(m[1]);
    } else buffer.push(line);
  }
  flush();
  return blocks;
}

/* ---------- preview form values ---------- */

export const oneLine = (text) => String(text || '').replace(CONTROL_RE, '').replace(/\s+/g, ' ').trim();
export const lineList = (text) => String(text || '').replace(CONTROL_RE, '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean);

const RICH = new Set(['description', 'observation', 'remediation']);
const LISTS = new Set(['urls', 'references']);

/** What a finding control shows for a field. */
export function controlText(finding, name) {
  if (name === 'poc') return pocToEditable(finding.poc);
  if (RICH.has(name)) return htmlToEditable(finding[name]);
  if (LISTS.has(name)) return finding[name].join('\n');
  return finding[name];
}

/** The edit value of a finding field from its control's text. */
export function fieldValue(name, text, imageKeys) {
  if (name === 'include') return text === true;
  if (name === 'poc') return editableToPoc(text, imageKeys);
  if (RICH.has(name)) return editableToHtml(text);
  if (LISTS.has(name)) return lineList(text);
  return name === 'severity' ? String(text) : oneLine(text);
}

/** The header values the form shows, in the edit-body shape. */
export function headerValues(form) {
  const email = oneLine(form.contact_email);
  return {
    company: oneLine(form.company), audit_name: oneLine(form.audit_name), language: form.language || '',
    audit_type: form.audit_type || '', template_id: form.template_id || '', date_start: form.date_start || '',
    date_end: form.date_end || '', scope: lineList(form.scope),
    contact: email ? { email, firstname: oneLine(form.contact_first), lastname: oneLine(form.contact_last) } : null,
  };
}

/** The data-fk of the control that fixes an issue ('' when there is none). */
export function issueTarget(issue) {
  const header = {
    company_missing: 'import-company', audit_name_missing: 'import-audit-name', language_missing: 'import-language',
    audit_type_missing: 'import-audit-type', template_missing: 'import-template', date_invalid: 'import-date-start',
    date_order: 'import-date-start', dates_missing: 'import-date-start', scope_missing: 'import-scope',
    contacts_absent: 'import-contact-email', duplicate_audit: 'import-audit-name', company_conflict: 'import-company',
    leftover_name: 'import-company',
  };
  if (header[issue.code]) return header[issue.code];
  if (issue.image_key) return 'img-' + issue.image_key + '-caption';
  if (!issue.finding_key) return '';
  const field = { severity_missing: 'severity', severity_label_mismatch: 'severity', cvss_missing: 'cvss_vector' }[issue.code] || (issue.code === 'fields_blank' && issue.fields[0]) || 'title';
  return 'f-' + issue.finding_key + '-' + field;
}

/** Readable names of the issue `fields` (the importer's own key names). */
export const FIELD_LABEL = {
  title: 'title', severity: 'severity', cvss_vector: 'CVSS vector', cvss_score: 'CVSS score', cwe: 'CWE / CVE',
  urls: 'vulnerable URLs', parameter: 'parameter', location: 'location', description: 'description',
  observation: 'observation', poc: 'proof of concept', remediation: 'remediation', references: 'references',
  include: 'include', company: 'company', audit_name: 'audit name', language: 'language', audit_type: 'audit type',
  template_id: 'template', date_start: 'start date', date_end: 'end date', scope: 'scope', contact: 'contact', images: 'screenshots',
};
export const fieldNames = (fields) => fields.map((f) => FIELD_LABEL[f] || f).join(', ');

export const blockers = (plan) => plan.issues.filter((i) => i.level === 'blocker');
export const warnings = (plan) => plan.issues.filter((i) => i.level === 'warning');

/** Whether "Create in PwnDoc" may be pressed, and why not. */
export function createGate(plan, { dirty, acceptBlanks, confirmDuplicate }) {
  if (dirty) return { ok: false, reason: 'Save your changes first.' };
  if (blockers(plan).length) return { ok: false, reason: 'Fix the items marked Required first.' };
  if (!plan.findings.some((f) => f.include)) return { ok: false, reason: 'Include at least one finding.' };
  if (plan.duplicate_audit && !confirmDuplicate) return { ok: false, reason: 'Confirm that you want another audit with the same name.' };
  if (warnings(plan).length && !acceptBlanks) return { ok: false, reason: 'Check the items above, then tick “Create with the blanks listed above”.' };
  return { ok: true, reason: '' };
}

/** What a create would make, for the confirm question. */
export function createSummary(plan) {
  const included = new Set(plan.findings.filter((f) => f.include).map((f) => f.key));
  const images = plan.images.filter((i) => i.include && i.usable && included.has(i.finding_key)).length;
  return { name: plan.header.audit_name, findings: included.size, images, company: plan.header.company, newCompany: !plan.company_id };
}

export const confirmText = (s) => 'Create audit “' + s.name + '” with ' + plural(s.findings, 'finding') + ' in PwnDoc?';

/** Counts of an undo (or rollback) result. */
export function undoCounts(undo) {
  const items = (undo && undo.items) || [];
  const count = (outcome, kind) => items.filter((i) => i.outcome === outcome && (!kind || i.kind === kind)).length;
  const keptImages = count('kept', 'image');
  return { deleted: count('deleted'), gone: count('gone'), failed: count('failed'), keptImages, keptOther: count('kept') - keptImages };
}

export const keptText = (n) => plural(n, 'screenshot') + (n === 1 ? ' stays' : ' stay')
  + ' in PwnDoc: another audit uses ' + (n === 1 ? 'it' : 'them') + ', or only an administrator can delete images.';

/** The company and contact an undo kept (it deletes them only when it can see nobody else uses them). */
export const keptOtherText = (n) => (n === 1 ? 'The company or contact this import created stays' : n + ' companies and contacts this import created stay')
  + ' in PwnDoc: other audits may still use ' + (n === 1 ? 'it' : 'them') + ', and only an administrator can check that.';
