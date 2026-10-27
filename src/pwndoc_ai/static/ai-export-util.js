/* pwndoc-ai: pure helpers of the "Export & fonts" drawer and the font pickers (no DOM).
 *
 * Mirrors of the server rules (docs/EXPORT_DESIGN.md): font names (font_catalog_model.valid_family),
 * sizes (parse_size), CSS stacks (valid_css_stack), the block types (block_types.py) and the plan
 * precedence (TypographyPlan.resolve). Everything the gateway sends is normalised here before the
 * drawer draws it; nothing here touches the DOM, so node unit tests import it directly. */
import { extOf, fmtBytes } from './ai-rag-files.js';
import { objects } from './ai-util.js';

export const MAX_EXPORT_DOCX_BYTES = 50 * 1024 * 1024;
export const MAX_EXPORT_PDF_BYTES = 100 * 1024 * 1024;
export const SIZE_MIN = 6;
export const SIZE_MAX = 72;
export const SIZE_PRESETS = [8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36];
export const PLAN_SAVE_MS = 600;
export const MAX_TERMS = 50;
export const MAX_PREVIEW_CHARS = 600;
export const OTHER = '__other__';
export const CUSTOM = 'custom';
const MAX_FAMILY_CHARS = 100;
const MAX_NAME_CHARS = 200;
const MAX_CSS_CHARS = 300;
const FORBIDDEN = new Set(':,\\="\';<>{}()[]'.split(''));
const CSS_BARE = /^[A-Za-z][A-Za-z0-9 -]{0,63}$/;
const CSS_QUOTED = /^"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}"$/;
const ID_RE = /^[0-9a-f]{32}$/;
const SECTION_RE = /^s[1-9][0-9]{0,4}$/;
const BLOCK_RE = /^b[0-9]{5}$/;
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

export const ENGINES = ['auto', 'structure', 'pdf2docx'];
export const ENGINE_LABEL = {
  auto: 'Auto (structure first, layout-faithful when needed)',
  structure: 'Structure rebuild (headings, lists, tables)',
  pdf2docx: 'Layout-faithful (pdf2docx)',
};
export const PDF2DOCX_NOTE = 'The layout-faithful engine uses PyMuPDF, which is licensed under the AGPL-3.0. It is optional and runs in a separate process.';
export const STATES = ['uploaded', 'converting', 'analysing', 'ready', 'error', 'cancelled'];
const RUNNING = new Set(['converting', 'analysing']);
export const isRunning = (state) => RUNNING.has(state);

export const BLOCK_TYPES = ['title', 'heading_1', 'heading_2', 'heading_3', 'heading_4', 'body', 'list', 'table',
  'code', 'caption', 'quote', 'toc', 'header', 'footer', 'footnote', 'figure', 'other'];
export const HEADING_TYPES = ['title', 'heading_1', 'heading_2', 'heading_3', 'heading_4'];
const DEFAULT_FAMILY_TYPES = new Set(BLOCK_TYPES.filter((t) => t !== 'code'));
const DEFAULT_SIZE_TYPES = new Set(['body', 'list', 'table', 'quote', 'other']);
export const BLOCK_TYPE_LABELS = {
  title: 'Title', heading_1: 'Heading 1', heading_2: 'Heading 2', heading_3: 'Heading 3',
  heading_4: 'Heading 4 and below', body: 'Body text', list: 'Lists', table: 'Tables', code: 'Code',
  caption: 'Captions', quote: 'Quotes', toc: 'Table of contents', header: 'Page header',
  footer: 'Page footer', footnote: 'Footnotes', figure: 'Figures', other: 'Other',
};
const STATUS_LABEL = { installed: 'installed', missing: 'missing' };

const REPORT_REASONS = {
  user_choice: 'the engine you chose', structure_ok: 'the structure rebuild was good enough',
  structure_failed: 'the structure rebuild failed', text_loss: 'the structure rebuild lost text',
  tabular: 'the PDF is table-heavy', sparse_pages: 'the pages are mostly images',
  columns: 'the PDF has more than two columns', fallback_unavailable: 'the layout-faithful engine is not installed',
  pdf2docx_worse: 'the layout-faithful engine lost more text',
};
const REPORT_WARNINGS = {
  mixed_scanned: 'Some pages have no text layer and were kept as images only.',
  fallback_unavailable: 'Auto wanted the layout-faithful engine, but it is not installed.',
  no_headings: 'The converter found no headings.',
  images_capped: 'There were too many images; some were skipped.',
  tables_split: 'Tables that continue over pages were kept as one table per page.',
  running_lines_removed: 'Repeated page headers and footers were removed.',
  tagged_pdf: 'The PDF carried structure tags.',
};
const SECTION_WARNINGS = {
  ai_disabled: 'Sections were detected without AI (switched off).',
  llm_unavailable: 'No language model is configured: sections were detected by rules only.',
  llm_failed: 'The AI model failed: the rest was detected by rules.',
  llm_key: 'The LLM rejected the API key; an administrator must fix it in AI Settings. Rules were used instead.',
  answer_rejected: 'Part of the AI answer was rejected and replaced by rules.',
  partial_coverage: 'The AI skipped some blocks; rules labelled them.',
  truncated: 'The document is very long: the end was labelled by rules.',
  no_headings: 'No headings were found: the whole document is one section.',
  sections_capped: 'There are very many headings: the later ones stay inside the last section.',
};

const str = (v, max) => (typeof v === 'string' ? v.slice(0, max || MAX_NAME_CHARS) : '');
const int = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);
const ratio = (v) => (typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
export const isFileId = (v) => typeof v === 'string' && ID_RE.test(v);
export const isSectionId = (v) => typeof v === 'string' && SECTION_RE.test(v);

/* ---------- font names, sizes, CSS stacks ---------- */

/** A family the gateway accepts: 1-100 printable characters, no font or CSS syntax, no leading '-'. */
export function validFamily(value) {
  if (typeof value !== 'string') return false;
  const name = value.replace(/\s+/g, ' ').trim();
  if (!name || name.length > MAX_FAMILY_CHARS || name.startsWith('-')) return false;
  return ![...name].some((ch) => FORBIDDEN.has(ch) || CONTROL.test(ch));
}

/** A typed family, whitespace collapsed ('' when blank or invalid). */
export const cleanFamily = (value) => (validFamily(value) ? value.replace(/\s+/g, ' ').trim() : '');

/** A size the plan accepts: a number in [6, 72] in steps of 0.5, else null. */
export function parseSize(value) {
  const n = typeof value === 'string' ? (value.trim() === '' ? NaN : Number(value)) : value;
  if (typeof n !== 'number' || !Number.isFinite(n) || n < SIZE_MIN || n > SIZE_MAX) return null;
  return n * 2 === Math.trunc(n * 2) ? n : null;
}

/** Settings sizes are lenient: clamped to [6, 72] and rounded to 0.5; 0, blank or negative is "off". */
export function clampSize(value) {
  const n = typeof value === 'string' ? (value.trim() === '' ? 0 : Number(value)) : value;
  if (typeof n !== 'number' || !Number.isFinite(n)) return null;
  if (n <= 0) return 0;
  return Math.max(SIZE_MIN, Math.min(SIZE_MAX, Math.floor(n * 2 + 0.5) / 2));
}

export function validCssStack(stack) {
  if (typeof stack !== 'string' || !stack || stack.length > MAX_CSS_CHARS) return false;
  return stack.split(',').map((t) => t.trim()).every((t) => CSS_QUOTED.test(t) || CSS_BARE.test(t));
}

/** The CSS stack of a free-text family (same rule as font_catalog_model.custom_css). */
export function customCss(family, category) {
  const generic = { serif: 'serif', mono: 'monospace' }[category] || 'sans-serif';
  const name = cleanFamily(family);
  const stack = name ? '"' + name + '", ' + generic : generic;
  return validCssStack(stack) ? stack : generic;
}

export const sizeText = (size) => (size == null ? '' : (Number.isInteger(size) ? String(size) : size.toFixed(1)) + ' pt');

/* ---------- the catalog ---------- */

function normFont(raw) {
  const family = cleanFamily(raw.family);
  if (!family) return null;
  const status = ['installed', 'substituted', 'missing'].includes(raw.status) ? raw.status : 'missing';
  const category = ['sans', 'serif', 'mono', 'display'].includes(raw.category) ? raw.category : 'sans';
  return {
    family, category, status,
    roles: Array.isArray(raw.roles) ? raw.roles.filter((r) => ['body', 'heading', 'mono'].includes(r)) : [],
    css: validCssStack(raw.css) ? raw.css : customCss(family, category),
    proprietary: raw.proprietary === true, licence: str(raw.licence, 60),
    resolved: cleanFamily(raw.resolved), layoutSafe: raw.layout_safe === true, custom: raw.custom === true,
  };
}

export function normCatalog(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const seen = new Set();
  const fonts = objects(data.fonts).map(normFont).filter((f) => {
    if (!f || seen.has(f.family.toLowerCase())) return false;
    seen.add(f.family.toLowerCase());
    return true;
  });
  const sizes = Array.isArray(data.sizes) ? data.sizes.map(parseSize).filter((n) => n !== null) : [];
  return { fonts, sizes: sizes.length ? sizes : SIZE_PRESETS.slice(), checked: data.checked !== false };
}

export const findFont = (catalog, family) => {
  const key = String(family || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  return (catalog && catalog.fonts.find((f) => f.family.toLowerCase().replace(/[^a-z0-9]/g, '') === key)) || null;
};

/** The CSS stack for a family: the catalog's, or a quoted custom name. */
export function cssFor(catalog, family, category) {
  const font = findFont(catalog, family);
  return font ? font.css : customCss(family, category);
}

/** Badge of a family: tone (ok/warn/bad/mute) and text. */
export function fontBadge(catalog, family) {
  if (!family) return null;
  const font = findFont(catalog, family);
  if (!font) return { tone: 'mute', text: 'not in the catalog' };
  if (font.status === 'installed') return { tone: 'ok', text: 'installed' };
  if (font.status === 'substituted') return { tone: 'warn', text: 'substituted by ' + font.resolved };
  return { tone: 'bad', text: STATUS_LABEL.missing };
}

/** One option label of the font dropdown. */
export function fontOptionText(font) {
  const badge = fontBadge({ fonts: [font] }, font.family);
  return font.family + ' (' + badge.text + ')';
}

/** "The server renders X as Y" sentences for the families of a spec list. */
export function substitutionNotes(catalog, families) {
  const out = [];
  for (const family of [...new Set(families.filter(Boolean))]) {
    const font = findFont(catalog, family);
    if (!font) out.push('“' + family + '” is not in the font catalog: the server may render it with another font.');
    else if (font.status === 'substituted') {
      out.push('The server renders ' + family + ' as ' + font.resolved + (font.layoutSafe ? ' (same metrics: line and page breaks match).' : ' (a close match: line breaks may differ).'));
    } else if (font.status === 'missing') out.push(family + ' is not installed on the server' + (font.resolved ? ': it renders as ' + font.resolved + '.' : '.'));
  }
  return out;
}

/** Fonts grouped for <optgroup>: [label, fonts] in a fixed order. */
export function fontGroups(catalog) {
  const groups = [['Sans serif', 'sans'], ['Serif', 'serif'], ['Monospace', 'mono'], ['Display', 'display']];
  return groups.map(([label, cat]) => [label, catalog.fonts.filter((f) => f.category === cat)]).filter(([, list]) => list.length);
}

/* ---------- picking a file ---------- */

export function checkExportPick(files) {
  if (!files || files.length === 0) return 'Choose a Word (.docx) or PDF file.';
  if (files.length > 1) return 'Choose one file at a time.';
  const file = files[0];
  const name = String((file && file.name) || '');
  if (!name || name.length > MAX_NAME_CHARS) return 'The file name is too long or missing.';
  const ext = extOf(name);
  if (ext === '.doc') return 'Older .doc files are not supported. Save the document as .docx first.';
  if (ext !== '.docx' && ext !== '.pdf') return 'Only Word (.docx) and PDF files are supported.';
  if (!(file.size > 0)) return 'The file is empty.';
  const limit = ext === '.pdf' ? MAX_EXPORT_PDF_BYTES : MAX_EXPORT_DOCX_BYTES;
  if (file.size > limit) return 'The file is larger than ' + fmtBytes(limit) + '.';
  return '';
}

export function parseTerms(text) {
  const seen = new Set();
  const terms = [];
  for (const piece of String(text || '').split(/[,\n]/)) {
    const term = piece.replace(/\s+/g, ' ').trim().slice(0, 100);
    if (!term || seen.has(term.toLowerCase())) continue;
    seen.add(term.toLowerCase());
    terms.push(term);
  }
  return terms.length > MAX_TERMS ? { terms: [], error: 'At most ' + MAX_TERMS + ' client names.' } : { terms, error: '' };
}

/* ---------- normalising answers ---------- */

export function normFile(raw) {
  if (!raw || !isFileId(raw.id)) return null;
  const progress = raw.progress && typeof raw.progress === 'object' ? raw.progress : {};
  return {
    id: raw.id, name: str(raw.name) || 'document', kind: raw.kind === 'pdf' ? 'pdf' : 'docx', size: int(raw.size),
    created: str(raw.created, 40), state: STATES.includes(raw.state) ? raw.state : 'error',
    engine: ENGINES.includes(raw.engine) ? raw.engine : 'auto', errorCode: str(raw.error_code, 40), error: str(raw.error, 300),
    done: int(progress.done), total: int(progress.total), planRev: int(raw.plan_rev),
  };
}

export const normFiles = (raw) => objects(raw && raw.files).map(normFile).filter(Boolean);

export function normQuota(raw) {
  const q = raw && raw.quota && typeof raw.quota === 'object' ? raw.quota : null;
  return q ? { used: int(q.used), maxBytes: int(q.max_bytes), files: int(q.files), maxFiles: int(q.max_files) } : null;
}

export function normStatus(raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  return {
    state: STATES.includes(d.state) ? d.state : 'error', done: int(d.done), total: int(d.total),
    step: ['converting', 'analysing'].includes(d.step) ? d.step : '', error: str(d.error, 300),
  };
}

export function normEngines(raw) {
  const list = objects(raw && raw.engines).filter((e) => ['structure', 'pdf2docx'].includes(e.name));
  const info = Object.fromEntries(list.map((e) => [e.name, { available: e.available === true, note: str(e.note, 40), licence: str(e.licence, 60) }]));
  return { info, ocr: !!(raw && raw.ocr === true) };
}

function normBlock(raw) {
  if (!BLOCK_RE.test(raw.id)) return null;
  return { id: raw.id, kind: raw.kind === 'table' ? 'table' : 'paragraph', text: str(raw.text, 300) };
}

function normSection(raw, blocks) {
  if (!isSectionId(raw.id)) return null;
  const counts = raw.type_counts && typeof raw.type_counts === 'object' ? raw.type_counts : {};
  return {
    id: raw.id, title: str(raw.title, 200).replace(/\s+/g, ' ').trim(),
    level: Math.min(9, int(raw.level)), headingBlock: BLOCK_RE.test(raw.heading_block) ? raw.heading_block : '',
    blockIds: Array.isArray(raw.block_ids) ? raw.block_ids.filter((id) => blocks.has(id)) : [],
    counts: BLOCK_TYPES.filter((t) => int(counts[t]) > 0).map((t) => [t, int(counts[t])]),
  };
}

export function normSections(raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  const blocks = new Map(objects(d.blocks).map(normBlock).filter(Boolean).map((b) => [b.id, b]));
  const types = new Map(objects(d.labels).filter((l) => BLOCK_TYPES.includes(l.type)).map((l) => [l.id, l.type]));
  return {
    method: ['ai', 'heuristic', 'mixed'].includes(d.method) ? d.method : 'heuristic', model: str(d.model, 100),
    warnings: (Array.isArray(d.warnings) ? d.warnings : []).filter((w) => SECTION_WARNINGS[w]),
    blocks, types, sections: objects(d.sections).map((s) => normSection(s, blocks)).filter(Boolean),
  };
}

/** The types present in the document, in BLOCK_TYPES order. */
export function presentTypes(sections) {
  const seen = new Set(sections.sections.flatMap((s) => s.counts.map(([t]) => t)));
  return BLOCK_TYPES.filter((t) => seen.has(t));
}

export const sectionLabel = (s) => s.title || (s.headingBlock ? '(untitled heading)' : 'Start of document');
export const methodText = (m, model) => ({ ai: 'Detected by AI' + (model ? ' (' + model + ')' : ''), heuristic: 'Detected by rules', mixed: 'Detected by AI and rules' }[m]);
export const sectionWarningText = (code) => SECTION_WARNINGS[code] || '';

/** Preview text of a section: its block excerpts, bounded. */
export function sectionText(sections, section) {
  const parts = [];
  let used = 0;
  for (const id of section.blockIds) {
    if (id === section.headingBlock) continue;
    const block = sections.blocks.get(id);
    if (!block || !block.text) continue;
    parts.push(block.text);
    used += block.text.length;
    if (used >= MAX_PREVIEW_CHARS) break;
  }
  const text = parts.join('\n');
  return text.length > MAX_PREVIEW_CHARS ? text.slice(0, MAX_PREVIEW_CHARS) + '…' : text;
}

/* ---------- the conversion report ---------- */

export function reportRows(raw) {
  if (!raw || typeof raw !== 'object' || !raw.metrics) return [];
  const m = raw.metrics;
  const heads = Array.isArray(m.headings) ? m.headings.map(int) : [];
  const lossPct = (ratio(m.text_loss) * 100).toFixed(1) + '%';
  return [
    ['Engine', ENGINE_LABEL[raw.engine_used] || String(raw.engine_used || '')],
    ['Why', REPORT_REASONS[raw.reason] || ''],
    ['Pages', String(int(raw.facts && raw.facts.pages))],
    ['Headings', String(heads.reduce((a, b) => a + b, 0) + int(m.title))],
    ['Tables', String(int(m.tables))],
    ['Images', String(int(m.images))],
    ['Text lost', lossPct],
  ].filter(([, v]) => v !== '');
}

export const reportWarnings = (raw) => (raw && Array.isArray(raw.warnings) ? raw.warnings.map((w) => REPORT_WARNINGS[w]).filter(Boolean) : []);

/* ---------- the typography plan ---------- */

const spec = (family, size) => ({ family: cleanFamily(family) || null, size: parseSize(size) });
const emptySpec = (s) => !s || (!s.family && s.size == null);
export const emptyPlan = () => ({ version: 1, default: { family: null, size: null }, by_block_type: {}, by_section: {} });

/** A plan as sent by the gateway, reduced to valid entries (unknown keys dropped). */
export function normPlan(raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  const plan = emptyPlan();
  if (d.default) plan.default = spec(d.default.family, d.default.size);
  for (const t of BLOCK_TYPES) {
    const s = d.by_block_type && d.by_block_type[t];
    if (s && !emptySpec(spec(s.family, s.size))) plan.by_block_type[t] = spec(s.family, s.size);
  }
  for (const [sid, style] of Object.entries((d.by_section && typeof d.by_section === 'object') ? d.by_section : {})) {
    if (!isSectionId(sid) || !style || typeof style !== 'object') continue;
    const all = style.all ? spec(style.all.family, style.all.size) : spec(null, null);
    const types = normPlan({ by_block_type: style.by_block_type }).by_block_type;
    if (!emptySpec(all) || Object.keys(types).length) plan.by_section[sid] = { all, by_block_type: types };
  }
  return plan;
}

/** The section-wide spec of a section ({family:null,size:null} when unset). */
export const sectionSpec = (plan, sid) => (plan.by_section[sid] ? plan.by_section[sid].all : { family: null, size: null });

/** A new plan with one spec replaced. `where` is {scope:'default'} | {scope:'type', type} | {scope:'section', sid}. */
export function withSpec(plan, where, next) {
  const value = spec(next.family, next.size);
  if (where.scope === 'default') return { ...plan, default: value };
  if (where.scope === 'type') {
    const types = { ...plan.by_block_type };
    if (emptySpec(value)) delete types[where.type];
    else types[where.type] = value;
    return { ...plan, by_block_type: types };
  }
  const sections = { ...plan.by_section };
  const old = sections[where.sid] || { all: spec(null, null), by_block_type: {} };
  const style = { ...old, all: value };
  if (emptySpec(style.all) && !Object.keys(style.by_block_type).length) delete sections[where.sid];
  else sections[where.sid] = style;
  return { ...plan, by_section: sections };
}

/** "Apply to all": the chosen section's font becomes the document default; section overrides go. */
export function applyToAll(plan, sid) {
  const chosen = sectionSpec(plan, sid);
  const merged = { family: chosen.family || plan.default.family, size: chosen.size != null ? chosen.size : plan.default.size };
  return { ...plan, default: spec(merged.family, merged.size), by_section: {} };
}

/** The effective spec of a block type in a section (TypographyPlan.resolve). */
export function resolveSpec(plan, type, sid) {
  const style = (sid && plan.by_section[sid]) || { all: spec(null, null), by_block_type: {} };
  const chain = [
    style.by_block_type[type],
    { family: DEFAULT_FAMILY_TYPES.has(type) ? style.all.family : null, size: DEFAULT_SIZE_TYPES.has(type) ? style.all.size : null },
    plan.by_block_type[type],
    { family: DEFAULT_FAMILY_TYPES.has(type) ? plan.default.family : null, size: DEFAULT_SIZE_TYPES.has(type) ? plan.default.size : null },
  ].filter(Boolean);
  const family = (chain.find((s) => s.family) || {}).family || null;
  const found = chain.find((s) => s.size != null);
  return { family, size: found ? found.size : null };
}

/** The plan as JSON for PUT (empty entries dropped). */
export function planBody(plan) {
  return normPlan(plan);
}

export const samePlan = (a, b) => JSON.stringify(normPlan(a)) === JSON.stringify(normPlan(b));

/** Every family the plan names (for the substitution notice). */
export function planFamilies(plan) {
  const specs = [plan.default, ...Object.values(plan.by_block_type)];
  for (const style of Object.values(plan.by_section)) specs.push(style.all, ...Object.values(style.by_block_type));
  return [...new Set(specs.map((s) => s && s.family).filter(Boolean))];
}

/** The heading type a section's title is drawn with. */
export const headingTypeOf = (section) => (section.headingBlock ? (section.level === 0 ? 'title' : 'heading_' + Math.min(4, Math.max(1, section.level))) : '');

/* ---------- download errors ---------- */
const DOWNLOAD_ERRORS = {
  409: 'Analyse this file before downloading it.',
  501: 'PDF download is not available: LibreOffice is not installed on the gateway. Word downloads still work.',
};
export const downloadFailureText = (status, format) => (status === 501 && format === 'docx' ? '' : DOWNLOAD_ERRORS[status] || '');
