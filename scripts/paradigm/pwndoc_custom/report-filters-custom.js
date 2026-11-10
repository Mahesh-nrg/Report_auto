// ParadigmIT VAPT template filters for PwnDoc (src/lib/custom/report-filters-custom.js).
// Only ADDS filters; the stock filters stay untouched, so other templates are unaffected.
//
// {@text | paradigmHTML:'Style'}
//   Same as the stock {@text | convertHTML:'Style'}, plus:
//   * every <ol> restarts at 1 (the stock converter shares one numbering instance across the
//     whole document, so finding 2's steps would continue at 6, 7, ...): ordered items become
//     paragraphs "1. text" with a bold number;
//   * <li> content without a <p> wrapper is kept (the stock converter silently drops it);
//   * empty trailing paragraphs are removed.
//
// {#findings | sortFindings} ... {/findings | sortFindings}
//   docs/FINDING_ORDER.md part A: Critical -> Informational by CVSS v3.1 base score (the band is
//   DERIVED from the score; the stored label only decides when there is no usable score), highest
//   score first, ties by the original identifier. Returns NEW objects (input untouched) with:
//   seq ('001'...), orderScore (number|null), orderBand, displaySeverity, severityMismatch,
//   scoreText ('9.8' / '0.0' / 'N/A'), cvssEstimated + estimatedNote, and the render-time N/A
//   fill (placeholder text -> 'N/A', placeholder rich chunks dropped so {^x}N/A{/x} shows,
//   Informational -> score 0.0 and cwecve 'N/A').
// {findings | bandCount:'Critical'}       number of findings whose DERIVED band is listed
//                                         (comma list, e.g. 'Medium,Low,Informational,Unknown')
// {#findings | inBand:'Critical,High'}    sorted findings of those bands (loops / inverted)
let expressions
try {
    expressions = require('angular-expressions')
} catch (e) {
    expressions = {filters: {}} // host-side unit tests: no PwnDoc node_modules
}

let defaultFilters = {}
let subTemplatingFilters = {}

function wrapListItems(html) {
    return html.replace(/<li>(?!\s*<p>)([\s\S]*?)<\/li>/g, '<li><p>$1</p></li>')
}

function numberOrderedLists(html) {
    return html.replace(/<ol[^>]*>([\s\S]*?)<\/ol>/g, function (match, inner) {
        let n = 0
        return inner.replace(/<li[^>]*>\s*<p>([\s\S]*?)<\/p>\s*<\/li>/g, function (m, text) {
            n += 1
            return '<p><strong>' + n + '.</strong> ' + text + '</p>'
        })
    })
}

// Linear on purpose: the anchored /(<p><\/p>)+$/ backtracks quadratically (ReDoS).
function stripTrailingEmpty(html) {
    let end = html.length
    while (end >= 7 && html.startsWith('<p></p>', end - 7)) end -= 7
    return html.slice(0, end)
}

defaultFilters.paradigmHTML = function (input, style) {
    const html2ooxml = require('../html2ooxml')
    if (typeof input !== 'string' || !input) return html2ooxml('', style)
    var html = numberOrderedLists(wrapListItems(input))
    html = stripTrailingEmpty(html)
    return html2ooxml(html, style)
}

// ---------------------------------------------------------------- CVSS v3.1 (part A section 1)

const BANDS = ['Critical', 'High', 'Medium', 'Low', 'Informational', 'Unknown']
const LABEL_ALIASES = {
    critical: 'Critical', high: 'High', medium: 'Medium', moderate: 'Medium', low: 'Low',
    info: 'Informational', informational: 'Informational', none: 'Informational'
}
const W = {
    AV: {N: 0.85, A: 0.62, L: 0.55, P: 0.2},
    AC: {L: 0.77, H: 0.44},
    UI: {N: 0.85, R: 0.62},
    C: {H: 0.56, L: 0.22, N: 0.0},
    I: {H: 0.56, L: 0.22, N: 0.0},
    A: {H: 0.56, L: 0.22, N: 0.0}
}
const PR = {U: {N: 0.85, L: 0.62, H: 0.27}, C: {N: 0.85, L: 0.68, H: 0.5}}
const ALLOWED = {AV: 'NALP', AC: 'LH', PR: 'NLH', UI: 'NR', S: 'UC', C: 'HLN', I: 'HLN', A: 'HLN'}
const NUMBER_RE = /^[0-9]+(\.[0-9]+)?$/
const COMMA_DECIMAL_RE = /^[0-9]{1,2},[0-9]$/
const X4 = 'XLMH', XM = 'XNLH'
const OPTIONAL = {E: 'XUPFH', RL: 'XOTWU', RC: 'XURC', CR: X4, IR: X4, AR: X4, MAV: 'XNALP',
    MAC: 'XLH', MPR: 'XNLH', MUI: 'XNR', MS: 'XUC', MC: XM, MI: XM, MA: XM}

function roundup31(value) {
    const asInt = Math.round(value * 100000)
    if (asInt % 10000 === 0) return asInt / 100000.0
    return (Math.floor(asInt / 10000) + 1) / 10.0
}

function roundup30(value) {
    return Math.ceil(value * 10) / 10
}

// '3.1', '3.0', '4', 'other' or null (no prefix / not a string)
function vectorVersion(vector) {
    if (typeof vector !== 'string') return null
    const head = vector.trim().toUpperCase().split('/')[0]
    if (!head.startsWith('CVSS:')) return null
    if (head === 'CVSS:3.1') return '3.1'
    if (head === 'CVSS:3.0') return '3.0'
    return head.startsWith('CVSS:4') ? '4' : 'other'
}

function parseMetrics(parts) {
    const metrics = {}
    for (const part of parts) {
        const at = part.indexOf(':')
        const name = at < 0 ? part : part.slice(0, at)
        const value = at < 0 ? '' : part.slice(at + 1)
        if (Object.prototype.hasOwnProperty.call(metrics, name)) return null
        const own = k => Object.prototype.hasOwnProperty.call(k, name)
        const allowed = own(ALLOWED) ? ALLOWED[name] : own(OPTIONAL) ? OPTIONAL[name] : null
        if (allowed === null || value.length !== 1 || allowed.indexOf(value) < 0) return null
        metrics[name] = value
    }
    return Object.keys(ALLOWED).every(k => k in metrics) ? metrics : null
}

function baseScore(m, roundup) {
    const iss = 1 - (1 - W.C[m.C]) * (1 - W.I[m.I]) * (1 - W.A[m.A])
    const changed = m.S === 'C'
    const impact = changed ? 7.52 * (iss - 0.029) - 3.25 * Math.pow(iss - 0.02, 15) : 6.42 * iss
    const expl = 8.22 * W.AV[m.AV] * W.AC[m.AC] * PR[m.S][m.PR] * W.UI[m.UI]
    if (impact <= 0) return 0.0
    const total = changed ? 1.08 * (impact + expl) : impact + expl
    return roundup(Math.min(total, 10))
}

function vectorScore(vector) {
    if (typeof vector !== 'string' || !vector.trim() || vector.length > 200) return null
    const version = vectorVersion(vector)
    if (version !== null && version !== '3.1' && version !== '3.0') return null
    let parts = vector.trim().toUpperCase().split('/')
    if (version !== null) parts = parts.slice(1)
    const metrics = parseMetrics(parts)
    if (!metrics) return null
    return baseScore(metrics, version === '3.0' ? roundup30 : roundup31)
}

function numericScore(raw) {
    let value
    if (typeof raw === 'number') value = raw
    else if (typeof raw === 'string') {
        let text = raw.trim()
        if (COMMA_DECIMAL_RE.test(text)) text = text.replace(',', '.')
        if (!NUMBER_RE.test(text)) return null
        value = parseFloat(text)
    } else return null
    if (!isFinite(value) || value < 0 || value > 10) return null
    return roundup31(value)
}

function scoreOf(f) {
    const fromVector = vectorScore(f.cvss_vector)
    if (fromVector !== null) return fromVector
    const version = vectorVersion(f.cvss_vector)
    if (version === '4' || version === 'other') return null
    return numericScore(f.cvss_score)
}

function bandOfScore(score) {
    if (score === 0) return 'Informational'
    if (score < 4.0) return 'Low'
    if (score < 7.0) return 'Medium'
    if (score < 9.0) return 'High'
    return 'Critical'
}

function bandOfLabel(label) {
    if (typeof label !== 'string') return 'Unknown'
    return LABEL_ALIASES[label.trim().toLowerCase()] || 'Unknown'
}

// ---------------------------------------------------------------- order (part A sections 2-4)

function compareKeys(a, b) {
    for (let i = 0; i < a.length; i++) {
        if (a[i] < b[i]) return -1
        if (a[i] > b[i]) return 1
    }
    return 0
}

function rankRow(f, position) {
    const score = scoreOf(f)
    const labelBand = bandOfLabel(f.severity)
    const band = score !== null ? bandOfScore(score) : labelBand
    const mismatch = score !== null && labelBand !== 'Unknown' && labelBand !== band
    const index = typeof f.index === 'number' ? f.index : position
    const key = [BANDS.indexOf(band), score === null ? 1 : 0, -(score || 0), index, position]
    return {key, position, score, band, mismatch}
}

// Abstract model (cvss_vector, cvss_score, severity, index) -> rows in report order with seq.
function orderAbstract(findings) {
    const rows = (findings || []).map(rankRow).sort((a, b) => compareKeys(a.key, b.key))
    const width = Math.max(3, String(rows.length).length)
    return rows.map((r, i) => ({
        position: r.position, score: r.score, band: r.band, mismatch: r.mismatch,
        seq: String(i + 1).padStart(width, '0')
    }))
}

// ---------------------------------------------------------------- PwnDoc data model adapter

const PLACEHOLDERS = new Set(['', '-', '--', '---', '–', '—', 'none', 'null', 'nil',
    'n/a', 'na', 'n.a.', 'tbd', 'undefined', 'not applicable'])
const TEXT_KEYS = ['title', 'vulnerablelocation', 'vulnerableurl', 'vulnerableparameter', 'cwecve']
const RICH_KEYS = ['description', 'observation', 'poc', 'remediation', 'stepstoreproduce']
const NA = 'N/A'

function isPlaceholder(value) {
    if (typeof value !== 'string') return true
    const text = value.replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/ /g, ' ')
    return PLACEHOLDERS.has(text.split(/\s+/).filter(Boolean).join(' ').toLowerCase())
}

function firstUsable(values) {
    return values.find(v => (typeof v === 'number') || (typeof v === 'string' && v.trim() !== ''))
}

function identifierIndex(identifier, position) {
    if (typeof identifier === 'number') return identifier
    const digits = typeof identifier === 'string' ? identifier.replace(/\D/g, '') : ''
    return digits ? parseInt(digits, 10) : position
}

// One rendered PwnDoc finding (prepAuditData output, container or 1.7 shape) -> abstract model.
function toAbstract(f, position) {
    const cvss = (f && typeof f.cvss === 'object' && f.cvss) || {}
    return {
        cvss_vector: firstUsable([f.cvssv3, cvss.vectorString, f.cvssVector]) || null,
        cvss_score: firstUsable([f.cvssscore, f.cvssScore, cvss.baseMetricScore]),
        severity: firstUsable([f.severity, f.cvssSeverity]) || null,
        index: identifierIndex(f.identifier, position)
    }
}

function isEstimated(f) {
    const flag = f.cvssestimated !== undefined ? f.cvssestimated : f.cvssEstimated
    if (flag === true) return true
    if (typeof flag === 'string' && /^(true|yes|1)$/i.test(flag.trim())) return true
    return typeof f.cvsssource === 'string' && /estimat|llm|heuristic/i.test(f.cvsssource)
}

function cleanRich(value) {
    if (!Array.isArray(value)) return value
    return value.filter(c => c && ((c.images && c.images.length) || !isPlaceholder(c.text)))
}

function cleanReferences(value) {
    return Array.isArray(value) ? value.filter(r => !isPlaceholder(r)) : value
}

function filledFields(f, info) {
    const out = {}
    for (const key of TEXT_KEYS) out[key] = isPlaceholder(f[key]) ? NA : f[key]
    for (const key of RICH_KEYS) if (key in f) out[key] = cleanRich(f[key])
    if ('references' in f) out.references = cleanReferences(f.references)
    if (info) out.cwecve = NA
    return out
}

function decorate(f, row) {
    const info = row.band === 'Informational'
    const score = info && row.score === null ? 0 : row.score
    const estimated = isEstimated(f)
    return Object.assign({}, f, filledFields(f, info), {
        seq: row.seq,
        orderScore: score,
        orderBand: row.band,
        displaySeverity: row.band === 'Unknown' ? NA : row.band,
        severityMismatch: row.mismatch,
        scoreText: score === null ? NA : score.toFixed(1),
        cvssEstimated: estimated,
        estimatedNote: estimated ? 'estimated by AI/heuristic - review' : ''
    })
}

function sortFindings(input) {
    if (!Array.isArray(input)) return []
    const items = input.filter(f => f && typeof f === 'object')
    const rows = orderAbstract(items.map(toAbstract))
    return rows.map(row => decorate(items[row.position], row))
}

function bandList(bands) {
    return String(bands || '').split(',').map(b => b.trim()).filter(Boolean)
}

function inBand(input, bands) {
    const wanted = bandList(bands)
    const sorted = Array.isArray(input) && input.every(f => f && 'orderBand' in f)
        ? input : sortFindings(input)
    return sorted.filter(f => wanted.indexOf(f.orderBand) >= 0)
}

defaultFilters.sortFindings = sortFindings
defaultFilters.inBand = inBand
defaultFilters.bandCount = function (input, bands) {
    return inBand(input, bands).length
}

Object.assign(expressions.filters, defaultFilters, subTemplatingFilters)
module.exports = {
    defaultFilters,
    subTemplatingFilters,
    expressions,
    stripTrailingEmpty,
    findingOrder: {orderAbstract, scoreOf, vectorScore, numericScore, bandOfLabel, toAbstract, isPlaceholder}
}
