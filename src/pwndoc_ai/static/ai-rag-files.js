/* pwndoc-ai: pure helpers of the knowledge-base panel (no DOM): file checks, formatting, and the
 * expansion of dropped folders into files. Importable from node, so it is unit tested there. */

export const ALLOWED_EXT = ['.pdf', '.txt', '.md', '.docx', '.json'];
export const DEFAULT_MAX_UPLOAD_BYTES = 300 * 1024 * 1024;  // the gateway's hard cap; it names the real one in the document list
export const MAX_DROP_FILES = 500;     // most files taken from one drop
const MAX_DROP_DEPTH = 8;              // folders nested deeper than this are not followed
const MAX_DIR_ENTRIES = 20000;         // most entries read from one folder
const MAX_NAME_CHARS = 200;
const KB = 1024;

/** Lower-case extension including the dot ('' when there is none; a leading dot is no extension). */
export function extOf(name) {
  const s = String(name || '');
  const i = s.lastIndexOf('.');
  return i > 0 ? s.slice(i).toLowerCase() : '';
}

export const fmtBytes = (n) => {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < KB) return n + ' B';
  if (n < KB * KB) return (n / KB).toFixed(1) + ' KB';
  if (n < KB * KB * KB) return (n / (KB * KB)).toFixed(1) + ' MB';
  return (n / (KB * KB * KB)).toFixed(1) + ' GB';
};

/** m:ss, or h:mm:ss from an hour on. */
export function fmtDuration(ms) {
  const total = Math.max(0, Math.floor((Number.isFinite(ms) ? ms : 0) / 1000));
  const s = String(total % 60).padStart(2, '0');
  const m = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  return hours ? hours + ':' + String(m).padStart(2, '0') + ':' + s : m + ':' + s;
}

/** A server timestamp (epoch seconds, epoch milliseconds or an ISO string) as milliseconds, or NaN. */
export function stampMs(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v < 1e11 ? Math.round(v * 1000) : v;
  if (typeof v === 'string' && v.trim()) return Date.parse(v);
  return Number.NaN;
}

/** Why a file cannot be uploaded, in words for the person ('' when it can). */
export function checkFile(file, maxBytes) {
  const name = String((file && file.name) || '');
  if (!name || name.length > MAX_NAME_CHARS) return 'The file name is too long or missing.';
  if (!ALLOWED_EXT.includes(extOf(name))) return 'This file type is not supported.';
  if (!(file.size > 0)) return 'The file is empty.';
  if (Number.isFinite(maxBytes) && file.size > maxBytes) return 'The file is larger than ' + fmtBytes(maxBytes) + '.';
  return '';
}

const fileOf = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));

async function readAll(reader) {
  const found = [];
  while (found.length < MAX_DIR_ENTRIES) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));  // 100 at a time
    if (!batch.length) break;
    found.push(...batch);
  }
  return found;
}

async function walk(entry, depth, acc) {
  if (acc.files.length >= acc.max) { acc.truncated = true; return; }
  try {
    if (entry.isFile) { acc.files.push(await fileOf(entry)); return; }
    if (!entry.isDirectory || depth > MAX_DROP_DEPTH) return;
    for (const child of await readAll(entry.createReader())) await walk(child, depth + 1, acc);
  } catch (e) {
    acc.unreadable += 1;  // counted and reported by the caller, never dropped silently
  }
}

/**
 * Files of a drop: folders are walked, plain files taken as they are. The entries are fetched
 * before the first await (the browser clears the drop data afterwards).
 * Returns { files, unreadable, truncated }.
 */
export async function collectDropped(dt, limits) {
  const max = (limits && limits.maxFiles) || MAX_DROP_FILES;
  const items = Array.from((dt && dt.items) || []).filter((i) => i && i.kind === 'file');
  const sources = items.map((i) => ({
    entry: typeof i.webkitGetAsEntry === 'function' ? i.webkitGetAsEntry() : null,
    file: typeof i.getAsFile === 'function' ? i.getAsFile() : null,
  }));
  const acc = { files: [], unreadable: 0, truncated: false, max };
  if (!sources.length) {
    const plain = Array.from((dt && dt.files) || []);
    return { files: plain.slice(0, max), unreadable: 0, truncated: plain.length > max };
  }
  for (const s of sources) {
    if (s.entry) await walk(s.entry, 0, acc);
    else if (s.file) acc.files.push(s.file);
  }
  return { files: acc.files, unreadable: acc.unreadable, truncated: acc.truncated };
}
