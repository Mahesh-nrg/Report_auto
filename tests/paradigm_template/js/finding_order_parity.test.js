#!/usr/bin/env node
// Parity of the PwnDoc sortFindings filter with docs/FINDING_ORDER.md part A test vectors.
//   node tests/paradigm_template/js/finding_order_parity.test.js
const assert = require('assert')
const path = require('path')
const fs = require('fs')

const ROOT = path.resolve(__dirname, '../../..')
const filters = require(path.join(ROOT, 'scripts/paradigm/pwndoc_custom/report-filters-custom.js'))
const {orderAbstract} = filters.findingOrder
const cases = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/data/finding_order_cases.json'), 'utf8')).cases

function project(findings) {
    return orderAbstract(findings).map(r => ({
        title: findings[r.position].title, score: r.score, band: r.band, seq: r.seq, mismatch: r.mismatch
    }))
}

let failed = 0
for (const c of cases) {
    try {
        assert.deepStrictEqual(project(c.findings), c.expected)
    } catch (e) {
        failed += 1
        console.error('MISMATCH', c.name, e.message.slice(0, 400))
    }
}

// PwnDoc-shaped data: derived band wins, identifiers tie-break, N/A fill, input untouched.
const pd = [
    {identifier: 'IDX-001', title: 'Info', severity: 'Info', cvssscore: '', cwecve: 'CWE-200', cvss: {vectorString: ''}},
    {identifier: 'IDX-002', title: 'Low label but 9.8', severity: 'Low', cvss: {vectorString: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H', baseMetricScore: '9.8'}, vulnerableurl: '-'},
    {identifier: 'IDX-003', title: 'Numeric 7,5', severity: 'High', cvssscore: '7,5', cvsssource: 'estimated_heuristic', description: [{text: '<p>-</p>', images: []}]},
    {identifier: 'IDX-004', title: 'No score crit', severity: 'Critical'}
]
const frozen = JSON.stringify(pd)
const out = filters.defaultFilters.sortFindings(pd)
assert.strictEqual(JSON.stringify(pd), frozen, 'input mutated')
assert.deepStrictEqual(out.map(f => f.title), ['Low label but 9.8', 'No score crit', 'Numeric 7,5', 'Info'])
assert.deepStrictEqual(out.map(f => f.seq), ['001', '002', '003', '004'])
assert.deepStrictEqual(out.map(f => f.orderBand), ['Critical', 'Critical', 'High', 'Informational'])
assert.strictEqual(out[0].severityMismatch, true)
assert.strictEqual(out[0].vulnerableurl, 'N/A')
assert.strictEqual(out[1].scoreText, 'N/A')
assert.strictEqual(out[2].scoreText, '7.5')
assert.strictEqual(out[2].cvssEstimated, true)
assert.deepStrictEqual(out[2].description, [])
assert.strictEqual(out[3].scoreText, '0.0')
assert.strictEqual(out[3].cwecve, 'N/A')
assert.strictEqual(filters.defaultFilters.bandCount(pd, 'Critical'), 2)
assert.strictEqual(filters.defaultFilters.bandCount(pd, 'Medium,Low,Informational,Unknown'), 1)
assert.deepStrictEqual(filters.defaultFilters.inBand(out, 'High').map(f => f.seq), ['003'])
assert.deepStrictEqual(filters.defaultFilters.sortFindings(null), [])

// Regressions FO-1 / FO-2 / WIRE-1.
{
    const fo = filters.findingOrder
    const b = 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'
    for (const bad of ['/E:Z', '/FOO:1', '/', '//E:P', '/MAV:Q', '/E']) assert.strictEqual(fo.vectorScore(b + bad), null, bad)
    assert.strictEqual(fo.vectorScore(b + '/E:P/RL:O/RC:C/CR:H/MAV:N/MS:X/MA:L'), 9.8)
    assert.strictEqual(fo.numericScore('1,234'), null)
    assert.strictEqual(fo.numericScore('7,5'), 7.5)
    const strip = filters.stripTrailingEmpty
    assert.strictEqual(strip('a<p></p><p></p>'), 'a')
    assert.strictEqual(strip('<p></p>x'), '<p></p>x')
    const t0 = Date.now()
    strip('<p></p>'.repeat(50000) + 'x')
    strip('a' + '<p></p>'.repeat(50000))
    assert.ok(Date.now() - t0 < 100, 'stripTrailingEmpty must be linear')
}

if (failed) { console.error(`${failed}/${cases.length} cases failed`); process.exit(1) }
console.log(`ok: ${cases.length} spec cases + PwnDoc adapter checks`)
