# ruff: noqa: E501, S108, S603, S607  -- embedded mongo-shell JS lines; fixed /tmp render harness; argv-list subprocess
"""Install / update / roll back the ParadigmIT VAPT PwnDoc report template.

Driven by install_pwndoc_template.sh and rollback_pwndoc_template.sh. All docker
calls go through ``$DOCKER`` (default ``docker``) as argument lists (no shell),
and every value handed to the mongo shell is JSON-encoded (ASCII-escaped) inside
a script piped on stdin, so names can never inject shell or JS code.

Read-only commands (inspect, find, ls) run even in --dry-run; every write is
only printed in --dry-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = Path(
    "/mnt/data/Pentesting/Report_automation/report_templates/generated/ParadigmIT_VAPT_PwnDoc_Template.docx"
)
MONGO = os.environ.get("PWNDOC_MONGO", "pwndoc-mongodb-1")
BACKEND = os.environ.get("PWNDOC_BACKEND", "pwndoc-pwndoc-backend-1")
FRONTEND = os.environ.get("PWNDOC_FRONTEND", "pwndoc-pwndoc-frontend-1")
TEMPLATES_IN_CONTAINER = "/app/report-templates"
HERE = Path(__file__).resolve().parent
RENDER_SH = HERE / "pwndoc_custom" / "run_render.sh"
RENDER_AUDIT = Path(
    "/mnt/data/Pentesting/Report_automation/report_templates/generated/rendered_samples/sshuffled.json"
)  # 19 findings stored in a shuffled order: the render must come out in CVSS v3.1 order
FILTER_SRC = HERE / "pwndoc_custom" / "report-filters-custom.js"
FILTER_NAME = "report-filters-custom.js"
FILTER_MARKER = "paradigmHTML"
# The installed filter file must define these (docs/FINDING_ORDER.md: sorted findings loops)
FILTER_REQUIRED = (
    "defaultFilters.paradigmHTML",
    "defaultFilters.sortFindings",
    "defaultFilters.bandCount",
)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")

# Table-driven: audit type defaults (mirrors the existing 'v1' audit type) and
# the custom fields the template needs. Edit here; existing docs are never changed.
HIGHLIGHTS_SECTION = {"field": "highlights", "name": "Assessment Highlights", "icon": "notes"}
AUDIT_TYPE_DEFAULTS = {
    "sections": ["Report starting", HIGHLIGHTS_SECTION["field"]],
    "hidden": [],
    "stage": "default",
}
LOCALES = ["1", "en"]
# Fields the template reads (PwnDoc keys them by the lower-cased, space-less label:
# applicationname, assessmenttype, targeturl, stepstoreproduce, highlights.text).
# Only created when no custom field with the same label+display+displaySub exists.
CUSTOM_FIELDS: list[dict] = [
    {"label": "Application Name", "fieldType": "input", "display": "general", "size": 6},
    {
        "label": "Assessment Type",
        "fieldType": "input",
        "display": "general",
        "size": 6,
        "description": "e.g. Grey-box",
    },
    {"label": "Target URL", "fieldType": "input", "display": "general", "size": 12},
    {"label": "Steps to Reproduce", "fieldType": "text", "display": "finding", "size": 12},
    {
        "label": "CVSS Source",
        "fieldType": "input",
        "display": "finding",
        "size": 4,
        "description": "vector / numeric / estimated_llm / estimated_heuristic (set by the gateway)",
    },
    {
        "label": "Text",
        "fieldType": "text",
        "display": "section",
        "displaySub": HIGHLIGHTS_SECTION["name"],
        "size": 12,
    },
]


class InstallError(RuntimeError):
    pass


def env_paths() -> dict[str, Path]:
    base = Path(os.environ.get("PWNDOC_BACKEND_DIR", str(REPO / "pwndoc" / "backend")))
    return {"templates": base / "report-templates", "backup": base / "backup"}


def docker_cmd() -> list[str]:
    return os.environ.get("DOCKER", "docker").split()


def run(
    args: list[str], *, stdin: str | None = None, binary: bool = False
) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603 - argv list, no shell
        docker_cmd() + args,
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"command failed ({proc.returncode}): docker {' '.join(args)}\n{proc.stderr.decode(errors='replace')}"
        )
    if not binary:
        proc.stdout = proc.stdout.decode(errors="replace")
    return proc


def js_value(value: object) -> str:
    """JSON literal safe to embed in a JS program (ASCII only, no </script> tricks)."""
    return json.dumps(value, ensure_ascii=True)


def mongo_eval(script: str) -> object:
    """Run a JS program in the pwndoc db; it must print one JSON line prefixed RESULT:."""
    out = run(["exec", "-i", MONGO, "mongo", "--quiet", "pwndoc"], stdin=script).stdout
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:") :])
    raise InstallError(f"no RESULT line from mongo:\n{out}")


def validate_name(name: str) -> None:
    if not SAFE_NAME.match(name) or ".." in name or name.strip() != name:
        raise InstallError(
            f"unsafe template name {name!r}: use letters, digits, space, _ . - (max 64)"
        )


def validate_docx(path: Path) -> None:
    if not path.is_file():
        raise InstallError(f"template not found: {path}")
    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                raise InstallError(f"{path} has no word/document.xml")
            if zf.testzip() is not None:
                raise InstallError(f"{path} has a corrupt member")
    except zipfile.BadZipFile as exc:
        raise InstallError(f"{path} is not a docx zip") from exc


def check_containers() -> None:
    names = run(["ps", "--format", "{{.Names}}"]).stdout.split()
    missing = [c for c in (MONGO, BACKEND, FRONTEND) if c not in names]
    if missing:
        raise InstallError(f"containers not running: {', '.join(missing)}")


def check_bind_mount(expected: Path) -> None:
    mounts = json.loads(run(["inspect", BACKEND, "--format", "{{json .Mounts}}"]).stdout or "[]")
    for m in mounts:
        if m.get("Destination") == TEMPLATES_IN_CONTAINER:
            if Path(m.get("Source", "")).resolve() != expected.resolve():
                raise InstallError(
                    f"{TEMPLATES_IN_CONTAINER} maps to {m.get('Source')}, expected {expected}"
                )
            return
    raise InstallError(f"{TEMPLATES_IN_CONTAINER} is not bind-mounted in {BACKEND}")


def existing_state(name: str) -> dict:
    script = f"""var n = {js_value(name)};
var t = db.templates.findOne({{name: n}});
var a = db.audittypes.findOne({{name: n}});
print('RESULT:' + JSON.stringify({{template: t ? t._id.str : null, audittype: a ? a._id.str : null,
  ping: db.runCommand({{ping: 1}}).ok}}));
"""
    return mongo_eval(script)


def preflight(opts: argparse.Namespace) -> dict:
    validate_name(opts.name)
    validate_docx(opts.template)
    check_containers()
    check_bind_mount(env_paths()["templates"])
    state = existing_state(opts.name)
    if state.get("ping") != 1:
        raise InstallError("mongo did not answer ping")
    target = env_paths()["templates"] / f"{opts.name}.docx"
    if opts.update:
        if not state["template"]:
            raise InstallError(f"--update: no template named {opts.name!r} exists; install first")
    elif state["template"] or state["audittype"] or target.exists():
        raise InstallError(
            f"name {opts.name!r} already used (template/audit type/file); use --update to replace the file"
        )
    print(f"[preflight] custom filter: {check_filter()}")
    print(
        f"[preflight] ok: containers up, mongo ping 1, bind mount ok, docx valid, target {target}"
    )
    return state


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backup(stamp: str, dry: bool) -> Path:
    bdir = env_paths()["backup"] / f"paradigm-{stamp}"
    print(f"[backup] mongodump --db pwndoc --archive --gzip -> {bdir}/pwndoc.archive.gz")
    print(f"[backup] report-templates listing + sha256 -> {bdir}/report-templates.sha256")
    if dry:
        return bdir
    bdir.mkdir(parents=True, exist_ok=False)
    dump = run(
        ["exec", MONGO, "mongodump", "--db", "pwndoc", "--archive", "--gzip", "--quiet"],
        binary=True,
    ).stdout
    (bdir / "pwndoc.archive.gz").write_bytes(dump)
    try:
        if len(gzip.decompress(dump)) < 64:
            raise InstallError("mongodump archive is empty")
    except OSError as exc:
        raise InstallError(f"mongodump archive is not valid gzip: {exc}") from exc
    lines = [
        f"{sha256(p)}  {p.name}" for p in sorted(env_paths()["templates"].iterdir()) if p.is_file()
    ]
    (bdir / "report-templates.sha256").write_text("\n".join(lines) + "\n")
    print(f"[backup] verified: archive {len(dump)} bytes, {len(lines)} template files listed")
    return bdir


def insert_script(name: str, fields: list[dict]) -> str:
    return f"""var n = {js_value(name)}, locales = {js_value(LOCALES)}, d = {js_value(AUDIT_TYPE_DEFAULTS)};
var fields = {js_value(fields)}, now = new Date(), created = {{customfields: []}};
if (db.templates.findOne({{name: n}}) || db.audittypes.findOne({{name: n}})) {{
  print('RESULT:' + JSON.stringify({{error: 'name already used'}})); quit(0);
}}
var tid = new ObjectId();
db.templates.insertOne({{_id: tid, name: n, ext: 'docx', createdAt: now, updatedAt: now, __v: 0}});
created.template = tid.str;
var last = db.audittypes.find().sort({{order: -1}}).limit(1).toArray();
var aid = new ObjectId();
db.audittypes.insertOne({{_id: aid, name: n,
  templates: locales.map(function (l) {{ return {{template: tid, locale: l}}; }}),
  sections: d.sections, hidden: d.hidden, stage: d.stage,
  order: (last.length ? (last[0].order || 0) : 0) + 1, createdAt: now, updatedAt: now, __v: 0}});
created.audittype = aid.str;
fields.forEach(function (f) {{
  if (db.customfields.findOne({{label: f.label, display: f.display, displaySub: f.displaySub || ''}})) return;
  var fid = new ObjectId(), doc = Object.assign({{displaySub: '', position: 0, size: 12, offset: 0,
    required: false, description: '', inline: false, options: [],
    text: locales.map(function (l) {{ return {{locale: l, value: ''}}; }})}}, f);
  doc._id = fid; doc.createdAt = now; doc.updatedAt = now; doc.__v = 0;
  db.customfields.insertOne(doc); created.customfields.push(fid.str);
}});
var sec = {js_value(HIGHLIGHTS_SECTION)};
if (!db.customsections.findOne({{$or: [{{field: sec.field}}, {{name: sec.name}}]}})) {{
  var lastSec = db.customsections.find().sort({{order: -1}}).limit(1).toArray();
  var sid = new ObjectId();
  db.customsections.insertOne({{_id: sid, field: sec.field, name: sec.name, icon: sec.icon,
    order: (lastSec.length ? (lastSec[0].order || 0) : 0) + 1, createdAt: now, updatedAt: now, __v: 0}});
  created.section = sid.str;
}}
print('RESULT:' + JSON.stringify(created));
"""


def filter_paths() -> tuple[Path, Path]:
    base = Path(os.environ.get("PWNDOC_BACKEND_DIR", str(REPO / "pwndoc" / "backend")))
    return FILTER_SRC, base / "src" / "lib" / "custom" / FILTER_NAME


def check_filter() -> str:
    """'install' | 'same' ; refuses a foreign custom filter file (never overwritten)."""
    src, dest = filter_paths()
    if not src.is_file():
        raise InstallError(f"custom filter source missing: {src}")
    if not dest.parent.is_dir():
        raise InstallError(f"{dest.parent} missing (expected the src/lib/custom bind mount)")
    if not dest.exists():
        return "install"
    text = dest.read_text(errors="replace")
    if FILTER_MARKER not in text:
        raise InstallError(
            f"{dest} exists and does not define {FILTER_MARKER}: merge "
            f"{src} into it by hand (the installer never overwrites foreign filters)"
        )
    return "same" if text == src.read_text() else "install"


def install_filter(bdir: Path, dry: bool) -> dict:
    """Copy the paradigmHTML filter into the bind-mounted src/lib/custom (backup first)."""
    src, dest = filter_paths()
    state = check_filter()
    print(f"[filter] {state}: {src} -> {dest}")
    if dry or state == "same":
        return {}
    record = {"filter": str(dest), "filter_backup": None}
    if dest.exists():
        saved = bdir / f"{FILTER_NAME}.orig"
        shutil.copyfile(dest, saved)
        record["filter_backup"] = str(saved)
    tmp = dest.with_name(dest.name + ".paradigm-tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    return record


def install_file(src: Path, name: str, dry: bool) -> Path:
    dest = env_paths()["templates"] / f"{name}.docx"
    print(f"[install] copy {src} -> {dest}")
    if not dry:
        tmp = dest.with_name(dest.name + ".paradigm-tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    return dest


def do_install(opts: argparse.Namespace, stamp: str, bdir: Path) -> dict:
    dest = install_file(opts.template, opts.name, opts.dry_run)
    print(f"[mongo] insert templates {{name:{opts.name!r}, ext:'docx'}}")
    print(
        f"[mongo] insert audittypes {{name:{opts.name!r}, locales {LOCALES}, {AUDIT_TYPE_DEFAULTS}}}"
    )
    print(
        f"[mongo] ensure {len(CUSTOM_FIELDS)} custom field(s): {[f['label'] for f in CUSTOM_FIELDS]}"
    )
    print(f"[mongo] ensure custom section {HIGHLIGHTS_SECTION}")
    created = {"name": opts.name, "stamp": stamp, **install_filter(bdir, opts.dry_run)}
    if opts.dry_run:
        return {}
    created.update({"file": str(dest), "file_sha256": sha256(dest)})
    # Record the file before the DB write so a failure can still be rolled back.
    (bdir / "created.json").write_text(json.dumps(created, indent=2))
    result = mongo_eval(insert_script(opts.name, CUSTOM_FIELDS))
    if "error" in result:
        raise InstallError(f"mongo refused: {result['error']}")
    created.update(result)
    (bdir / "created.json").write_text(json.dumps(created, indent=2))
    print(f"[install] recorded ids in {bdir / 'created.json'}")
    return created


def backup_template_file(name: str, bdir: Path, dry: bool) -> str | None:
    """Copy the currently installed template .docx into the backup dir (update rollback)."""
    current = env_paths()["templates"] / f"{name}.docx"
    print(f"[backup] {current} -> {bdir / (current.name + '.orig')}")
    if dry or not current.is_file():
        return None
    saved = bdir / f"{current.name}.orig"
    shutil.copyfile(current, saved)
    return str(saved)


def do_update(opts: argparse.Namespace, state: dict, bdir: Path) -> None:
    """Replace the template file AND refresh the custom filter file; both backed up first and
    recorded in updated.json so ``rollback --backup-dir <dir>`` restores them (ids unchanged)."""
    file_backup = backup_template_file(opts.name, bdir, opts.dry_run)
    dest = install_file(opts.template, opts.name, opts.dry_run)
    record = install_filter(bdir, opts.dry_run)
    if not opts.dry_run:
        update = {
            "kind": "update",
            "name": opts.name,
            "file": str(dest),
            "file_backup": file_backup,
            "file_sha256": sha256(dest),
            **record,
        }
        (bdir / "updated.json").write_text(json.dumps(update, indent=2))
        print(f"[update] recorded {bdir / 'updated.json'}")
    print(f"[mongo] templates {state['template']}: set updatedAt=now (ids unchanged)")
    if opts.dry_run:
        return
    script = f"""var r = db.templates.updateOne({{_id: ObjectId({js_value(state["template"])})}},
  {{$set: {{updatedAt: new Date()}}}});
print('RESULT:' + JSON.stringify({{matched: r.matchedCount}}));
"""
    if mongo_eval(script).get("matched") != 1:
        raise InstallError("template document vanished during update")


def verify(name: str, dry: bool) -> None:
    script = f"""var n = {js_value(name)};
var t = db.templates.findOne({{name: n}}), a = db.audittypes.findOne({{name: n}});
print('RESULT:' + JSON.stringify({{template: t ? t._id.str : null, ext: t ? t.ext : null,
  audittype: a ? a._id.str : null,
  linked: a && t ? a.templates.filter(function (x) {{ return x.template.str === t._id.str; }}).length : 0}}));
"""
    print(
        f"[verify] find() template + audit type {name!r}; ls -la {TEMPLATES_IN_CONTAINER} in {BACKEND}"
    )
    if dry:
        return
    res = mongo_eval(script)
    if not res.get("template") or not res.get("audittype") or res.get("linked") != len(LOCALES):
        raise InstallError(f"verification failed: {res}")
    listing = run(["exec", BACKEND, "ls", "-la", TEMPLATES_IN_CONTAINER]).stdout
    if f"{name}.docx" not in listing:
        raise InstallError(f"{name}.docx not visible inside {BACKEND}")
    print(f"[verify] ok: {res}")
    verify_installed_filter()
    render_check(env_paths()["templates"] / f"{name}.docx")


def verify_installed_filter() -> None:
    """The bind-mounted filter file the backend loads must define the sort/count filters."""
    _, dest = filter_paths()
    text = dest.read_text(errors="replace") if dest.is_file() else ""
    missing = [name for name in FILTER_REQUIRED if name not in text]
    if missing:
        raise InstallError(f"installed filter {dest} lacks {missing}")
    print(f"[verify] installed filter defines {[n.split('.')[-1] for n in FILTER_REQUIRED]}")


def render_check(path: Path) -> None:
    if not RENDER_SH.exists() or docker_cmd() != ["docker"]:
        print("[verify] offline render skipped (render harness absent or DOCKER overridden)")
        return
    out = Path("/tmp/paradigm/installed_check.docx")
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["sh", str(RENDER_SH), str(path), str(out)]
    if RENDER_AUDIT.is_file():
        args.append(str(RENDER_AUDIT))
    # no --filters: the render loads the filter from the INSTALLED path (src/lib/custom mount)
    proc = subprocess.run(args, capture_output=True, check=False)  # noqa: S603,S607
    if proc.returncode != 0 or not out.exists():
        status = f"FAILED: {proc.stderr.decode(errors='replace')[-400:]}"
    else:
        status = order_status(out)
    print(f"[verify] offline render with the container generator: {status}")


def order_status(out: Path) -> str:
    """'ok' when the rendered findings follow docs/FINDING_ORDER.md (pwt_order_check)."""
    if not RENDER_AUDIT.is_file():
        return "ok (rendered; no sample audit for the order check)"
    sys.path.insert(0, str(HERE))
    from pwt_order_check import check_docx, docx_view, expected_order

    expected = expected_order(json.loads(RENDER_AUDIT.read_text(encoding="utf-8")))
    problems = check_docx(expected, docx_view(out))
    if problems:
        return f"FAILED order check: {problems[:3]}"
    return f"ok, {len(expected)} findings in CVSS v3.1 order (seq 001..)"


def restart_backend(dry: bool) -> None:
    """report-filters-custom.js is required once at backend start-up."""
    print(f"[services] docker restart {BACKEND} (loads the {FILTER_MARKER} filter)")
    if not dry:
        run(["restart", BACKEND])


def services(action: str, dry: bool) -> None:
    print(f"[services] docker {action} {BACKEND} {FRONTEND} (mongo stays up)")
    if not dry:
        run([action, BACKEND, FRONTEND])


def find_record(name: str, backup_dir: Path | None) -> Path:
    """created.json (install) or updated.json (update) of ``backup_dir``, else the newest
    install/update record for ``name`` under the backup root."""
    if backup_dir:
        for rec_name in ("updated.json", "created.json"):
            if (backup_dir / rec_name).is_file():
                return backup_dir / rec_name
        raise InstallError(f"no created.json/updated.json in {backup_dir}")
    root = env_paths()["backup"]
    recs = sorted(
        [*root.glob("paradigm-*/created.json"), *root.glob("paradigm-*/updated.json")],
        key=lambda r: r.parent.name,
    )
    recs = [r for r in recs if json.loads(r.read_text()).get("name") == name]
    if not recs:
        raise InstallError(f"no installer record for {name!r} under {env_paths()['backup']}")
    return recs[-1]


def rollback_script(rec: dict) -> str:
    return f"""var r = {js_value(rec)}, out = {{templates: 0, audittypes: 0, customfields: 0}};
function oid(s) {{ return ObjectId(s); }}
if (r.audittype) out.audittypes = db.audittypes.deleteOne({{_id: oid(r.audittype), name: r.name}}).deletedCount;
if (r.template) out.templates = db.templates.deleteOne({{_id: oid(r.template), name: r.name}}).deletedCount;
if (r.section) out.customsections = db.customsections.deleteOne({{_id: oid(r.section)}}).deletedCount;
(r.customfields || []).forEach(function (f) {{ out.customfields += db.customfields.deleteOne({{_id: oid(f)}}).deletedCount; }});
print('RESULT:' + JSON.stringify(out));
"""


def restore_filter(rec: dict) -> None:
    """Put back the custom filter file the install replaced, or remove the one it added."""
    _, dest = filter_paths()
    if rec.get("filter") != str(dest):
        return
    backup_file = rec.get("filter_backup")
    if backup_file and Path(backup_file).is_file():
        shutil.copyfile(backup_file, dest)
        print(f"[rollback] restored {dest} from {backup_file}")
    elif dest.exists():
        dest.unlink()
        print(f"[rollback] removed {dest}")
    restart_backend(dry=False)


def rollback_update(path: Path, rec: dict, dry: bool) -> int:
    """Undo an --update: put back the previous template file and filter file, restart."""
    expected = env_paths()["templates"] / f"{rec['name']}.docx"
    saved = rec.get("file_backup")
    print(f"[rollback] update record {path}: restore {expected} from {saved}, filter from backup")
    if dry:
        return 0
    if rec.get("file") == str(expected) and saved and Path(saved).is_file():
        shutil.copyfile(saved, expected)
        print(f"[rollback] restored {expected}")
    if rec.get("filter"):
        restore_filter(rec)  # restarts the backend
    else:
        restart_backend(dry=False)
    rec["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path.write_text(json.dumps(rec, indent=2))
    return 0


def do_rollback(opts: argparse.Namespace) -> int:
    validate_name(opts.name)
    path = find_record(opts.name, opts.backup_dir)
    rec = json.loads(path.read_text())
    if rec.get("kind") == "update":
        return rollback_update(path, rec, opts.dry_run)
    keys = {"name", "template", "audittype", "customfields", "section"}
    safe = {k: v for k, v in rec.items() if k in keys}
    ids = [safe.get("template"), safe.get("audittype"), safe.get("section")]
    for oid in [*ids, *safe.get("customfields", [])]:
        if oid is not None and not re.fullmatch(r"[0-9a-f]{24}", str(oid)):
            raise InstallError(f"bad id in {path}: {oid!r}")
    expected = env_paths()["templates"] / f"{rec['name']}.docx"
    print(f"[rollback] record {path}")
    print(f"[rollback] delete ONLY ids {safe} and file {expected}")
    if opts.dry_run:
        return 0
    print(f"[rollback] mongo deleted: {mongo_eval(rollback_script(safe))}")
    if rec.get("file") == str(expected) and expected.exists():
        expected.unlink()
        print(f"[rollback] removed {expected}")
    restore_filter(rec)
    rec["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path.write_text(json.dumps(rec, indent=2))
    return 0


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["install", "rollback"])
    p.add_argument("--apply", action="store_true", help="really write (default is --dry-run)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--update", action="store_true", help="replace only the file, bump updatedAt")
    p.add_argument("--stop-services", action="store_true")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    p.add_argument("--name", default="ParadigmIT VAPT")
    p.add_argument("--backup-dir", type=Path)
    opts = p.parse_args(argv)
    if opts.apply and opts.dry_run:
        p.error("--apply and --dry-run are exclusive")
    opts.dry_run = not opts.apply
    return opts


def install(opts: argparse.Namespace) -> int:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    print(
        f"=== {'DRY RUN' if opts.dry_run else 'APPLY'}: install {opts.template} as {opts.name!r} ==="
    )
    state = preflight(opts)
    bdir = backup(stamp, opts.dry_run)
    stopped = False
    try:
        if opts.stop_services:
            services("stop", opts.dry_run)
            stopped = True
        if opts.update:
            do_update(opts, state, bdir)
        else:
            do_install(opts, stamp, bdir)
    finally:
        if stopped:
            services("start", opts.dry_run)
    if not stopped:
        restart_backend(opts.dry_run)
    verify(opts.name, opts.dry_run)
    here = Path(__file__).resolve().parent
    print(f"Audit type name: {opts.name}")
    print(
        f"Rollback: {here / 'rollback_pwndoc_template.sh'} --apply --name {json.dumps(opts.name)} --backup-dir {bdir}"
    )
    print(
        f"Full DB restore (last resort): docker exec -i {MONGO} mongorestore --drop --gzip --archive < {bdir}/pwndoc.archive.gz"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    opts = parse(sys.argv[1:] if argv is None else argv)
    try:
        return install(opts) if opts.command == "install" else do_rollback(opts)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
