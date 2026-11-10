#!/usr/bin/env node
/*
 * Offline render of a PwnDoc report template with the REAL generator (src/lib/report-generator.js
 * of the running backend image), without any database access and writing only where told.
 *
 *   node render_template.js <template.docx> <audit.json> <out.docx> [--dump data.json] [--filters custom.js]
 *   --filters injects a module as src/lib/custom/report-filters-custom.js (that dir is a read-only bind mount
 *   in production; this lets you test a custom filter file before dropping it there).
 *
 * Environment: PWNDOC_APP (default /app = backend root inside the container).
 *
 * How it stays DB-free while running unmodified generator code:
 *  - autoIndex/autoCreate/bufferCommands are disabled and no connection is ever opened;
 *  - the two mongoose statics the generator calls (Settings.getAll, Image.getOne) are replaced by
 *    in-memory stubs fed from the audit JSON (`_settings`, `_images`);
 *  - the generator builds the template path as `${__basedir}/../report-templates/${name}.${ext}`,
 *    so a relative name ("../../tmp/x/tpl") lets it read a template from anywhere;
 *  - the audit is a plain object shaped like Audit.getAudit() output (populated), with
 *    scope.toObject() added because the generator calls it on the mongoose array.
 */
const fs = require('fs')
const path = require('path')

const APP = process.env.PWNDOC_APP || '/app'
global.__basedir = path.join(APP, 'src')

// Default settings = the values stored in the live DB (settings collection), image border on.
const DEFAULT_SETTINGS = {
    report: {
        enabled: true,
        public: {
            cvssColors: {noneColor: '#4a86e8', lowColor: '#008000', mediumColor: '#f9a009',
                highColor: '#fe0000', criticalColor: '#212121'},
            captions: ['Figure'], highlightWarning: false, highlightWarningColor: '#ffff25'
        },
        private: {imageBorder: true, imageBorderColor: '#000000'}
    },
    reviews: {enabled: false}
}

function parseArgs(argv) {
    const args = argv.slice(2)
    const dumpIdx = args.indexOf('--dump')
    const dump = dumpIdx >= 0 ? args.splice(dumpIdx, 2)[1] : null
    const fIdx = args.indexOf('--filters')
    const filters = fIdx >= 0 ? path.resolve(args.splice(fIdx, 2)[1]) : null
    if (args.length < 3) {
        console.error('usage: render_template.js <template.docx> <audit.json> <out.docx> [--dump data.json]')
        process.exit(2)
    }
    return {template: path.resolve(args[0]), auditFile: path.resolve(args[1]), out: path.resolve(args[2]), dump, filters}
}

function loadModels() {
    const mongoose = require(path.join(APP, 'node_modules', 'mongoose'))
    mongoose.set('autoIndex', false)
    mongoose.set('autoCreate', false)
    mongoose.set('bufferCommands', false)
    return {
        Settings: require(path.join(APP, 'src/models/settings')),
        Image: require(path.join(APP, 'src/models/image')),
    }
}

function installStubs({Settings, Image}, raw) {
    const settings = JSON.parse(JSON.stringify(raw._settings || DEFAULT_SETTINGS))
    const images = raw._images || {}
    Settings.getAll = async () => settings
    Image.getOne = async (id) => {
        if (!images[id]) throw {fn: 'NotFound', message: 'Image not found'}
        return {_id: id, value: images[id], name: id}
    }
}

function buildAudit(raw, templatePath) {
    const audit = JSON.parse(JSON.stringify(raw))
    delete audit._images; delete audit._settings; delete audit._note
    const noExt = templatePath.replace(/\.[^.\\/]+$/, '')
    audit.template = {
        name: path.relative(path.join(APP, 'report-templates'), noExt),
        ext: path.extname(templatePath).slice(1) || 'docx'
    }
    audit.scope = Object.assign(audit.scope || [], {toObject() { return this.map(s => ({...s})) }})
    return audit
}

// Capture the exact object handed to docxtemplater (= the data model visible to templates)
function installDumper(file) {
    const Docxtemplater = require(path.join(APP, 'node_modules', 'docxtemplater'))
    const original = Docxtemplater.prototype.render
    Docxtemplater.prototype.render = function (data) {
        const clean = JSON.stringify(data, (k, v) =>
            typeof v === 'string' && v.startsWith('data:image') ? `<${v.length} bytes of image data URI>` : v, 1)
        fs.writeFileSync(file, clean)
        return original.call(this, data)
    }
}

function installFilters(file) {
    const Module = require('module')
    const target = path.join(APP, 'src/lib/custom/report-filters-custom.js')
    const origLoad = Module._load
    Module._load = function (request, parent, isMain) {
        if (/report-filters-custom(\.js)?$/.test(request)) return origLoad.call(this, file, parent, isMain)
        return origLoad.apply(this, arguments)
    }
    return target
}

async function main() {
    const opts = parseArgs(process.argv)
    const raw = JSON.parse(fs.readFileSync(opts.auditFile, 'utf8'))
    const models = loadModels()
    installStubs(models, raw)
    if (opts.dump) installDumper(opts.dump)
    if (opts.filters) installFilters(opts.filters)
    const generator = require(path.join(APP, 'src/lib/report-generator.js'))
    const audit = buildAudit(raw, opts.template)
    const buf = await generator.generateDoc(audit)
    fs.writeFileSync(opts.out, buf)
    console.log(`rendered ${path.basename(opts.template)} -> ${opts.out} (${buf.length} bytes)`)
}

main().then(() => process.exit(0)).catch(err => {
    console.error('RENDER FAILED:', typeof err === 'string' ? err : (err && err.stack) || err)
    process.exit(1)
})
