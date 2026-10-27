/* pwndoc-ai: calls to the gateway API. Every request has a timeout. */
import { API, REQUEST_TIMEOUT_MS, UPLOAD_TIMEOUT_MS } from './ai-util.js';

class ApiError extends Error {
  constructor(message, status, aborted) { super(message); this.status = status; this.aborted = aborted === true; }
}

export function detailText(data) {
  const d = data && (data.detail !== undefined ? data.detail : data.message);
  if (Array.isArray(d)) return d.map((x) => (x && x.msg) || String(x)).join('; ');
  return d == null ? '' : typeof d === 'string' ? d : JSON.stringify(d);
}

const timeoutSignal = (ms) => (typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(ms) : undefined);

/** fetch against the gateway: same-origin cookies, the CSRF header, and a hard timeout. */
export async function fetchApi(path, init, timeoutMs) {
  // Always sent: some GET routes (/qa, /history, /report) cost real work and require it too.
  const headers = Object.assign({ 'X-Requested-With': 'pwndoc-ai' }, init && init.headers);
  try {
    return await fetch(API + path, Object.assign({}, init, {
      headers, credentials: 'same-origin', signal: timeoutSignal(timeoutMs || REQUEST_TIMEOUT_MS),
    }));
  } catch (e) {
    if (e && e.name === 'TimeoutError') throw new ApiError('The gateway did not answer in time', 0);
    throw new ApiError('Cannot reach the AI gateway', 0);
  }
}

export async function api(path, opts) {
  const { method = 'GET', body, form, timeoutMs } = opts || {};
  const headers = {};
  let payload;
  if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  if (form) payload = form;
  const res = await fetchApi(path, { method, headers, body: payload }, timeoutMs);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  if (!res.ok) throw new ApiError(detailText(data) || res.statusText || 'Request failed', res.status);
  return data;
}

function parseBody(xhr) {
  try { return JSON.parse(xhr.responseText); } catch (e) { return null; }  // non-JSON body
}

/**
 * Upload a FormData with real progress (fetch cannot report upload progress). Same rules as
 * api(): same-origin cookies, the CSRF header, a hard timeout. `onProgress(loaded, total)` runs as
 * bytes go out; aborting `signal` rejects with an error whose `aborted` is true.
 */
export function uploadApi(path, form, opts) {
  const { onProgress, signal, timeoutMs } = opts || {};
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const fail = (message, status, aborted) => reject(new ApiError(message, status, aborted));
    if (signal && signal.aborted) { fail('Cancelled', 0, true); return; }
    xhr.open('POST', API + path);
    xhr.withCredentials = true;
    xhr.timeout = timeoutMs || UPLOAD_TIMEOUT_MS;
    xhr.setRequestHeader('X-Requested-With', 'pwndoc-ai');
    xhr.upload.onprogress = (e) => { if (onProgress && e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      const data = parseBody(xhr);
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else fail(detailText(data) || xhr.statusText || 'Upload failed', xhr.status, false);
    };
    xhr.onerror = () => fail('Cannot reach the AI gateway', 0, false);
    xhr.ontimeout = () => fail('The gateway did not answer in time', 0, false);
    xhr.onabort = () => fail('Cancelled', 0, true);
    if (signal) signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(form);
  });
}
