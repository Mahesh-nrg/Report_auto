"""Installer/rollback tests against a docker-less stub (DOCKER=<stub>)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "paradigm"
INSTALL = SCRIPTS / "install_pwndoc_template.sh"
ROLLBACK = SCRIPTS / "rollback_pwndoc_template.sh"

STUB = r"""
import gzip, json, os, sys
st_path = os.environ["STUB_STATE"]
st = json.load(open(st_path))
args = sys.argv[1:]
stdin = sys.stdin.read() if args[:2] == ["exec", "-i"] else ""
st["log"].append({"args": args, "stdin": stdin})
def done(out=""):
    json.dump(st, open(st_path, "w")); sys.stdout.write(out); sys.exit(0)
if args[0] == "ps":
    done("\n".join(st["containers"]) + "\n")
if args[0] == "inspect":
    done(json.dumps([{"Destination": "/app/report-templates", "Source": st["mount"]}]))
if args[0] in ("stop", "start", "restart"):
    done()
if "mongodump" in args:
    sys.stdout.flush(); sys.stdout.buffer.write(gzip.compress(b"x" * 200)); done()
if args[:1] == ["exec"] and "ls" in args:
    done("\n".join(os.listdir(st["mount"])))
if "mongo" in args:
    name = json.loads(stdin.split("var n = ", 1)[1].split(";", 1)[0].split(", locales")[0]) if "var n = " in stdin else None
    if "insertOne" in stdin:
        if name in st["names"]:
            done('RESULT:{"error":"name already used"}\n')
        st["names"].append(name); st["docs"] += ["a" * 24, "b" * 24]
        done('RESULT:' + json.dumps({"template": "a" * 24, "audittype": "b" * 24, "customfields": []}) + "\n")
    if "deleteOne" in stdin:
        rec = json.loads(stdin.split("var r = ", 1)[1].split(", out =", 1)[0])
        ids = [rec.get("template"), rec.get("audittype")] + rec.get("customfields", [])
        n = sum(1 for i in ids if i in st["docs"])
        st["docs"] = [d for d in st["docs"] if d not in ids]
        done('RESULT:' + json.dumps({"templates": n}) + "\n")
    if "updateOne" in stdin:
        done('RESULT:{"matched":1}\n')
    if "linked" in stdin:
        ok = name in st["names"]
        done('RESULT:' + json.dumps({"template": "a" * 24 if ok else None, "ext": "docx",
             "audittype": "b" * 24 if ok else None, "linked": 2 if ok else 0}) + "\n")
    done('RESULT:' + json.dumps({"template": "a" * 24 if name in st["names"] else None,
         "audittype": None, "ping": 1}) + "\n")
sys.exit(1)
"""


@pytest.fixture()
def env(tmp_path: Path) -> dict:
    backend = tmp_path / "backend"
    (backend / "report-templates").mkdir(parents=True)
    (backend / "backup").mkdir()
    (backend / "report-templates" / "v1.docx").write_bytes(b"existing")
    (backend / "src" / "lib" / "custom").mkdir(parents=True)
    stub = tmp_path / "docker_stub.py"
    stub.write_text(STUB)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "log": [],
                "names": ["v1"],
                "docs": ["c" * 24],
                "containers": [
                    "pwndoc-mongodb-1",
                    "pwndoc-pwndoc-backend-1",
                    "pwndoc-pwndoc-frontend-1",
                ],
                "mount": str(backend / "report-templates"),
            }
        )
    )
    tpl = tmp_path / "tpl.docx"
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    e = dict(
        os.environ,
        DOCKER=f"{sys.executable} {stub}",
        STUB_STATE=str(state),
        PWNDOC_BACKEND_DIR=str(backend),
        PYTHON=sys.executable,
    )
    return {"env": e, "state": state, "backend": backend, "tpl": tpl}


def run(script: Path, ctx: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], env=ctx["env"], capture_output=True, text=True, check=False
    )


def log(ctx: dict) -> list[dict]:
    return json.loads(ctx["state"].read_text())["log"]


def test_dry_run_writes_nothing(env: dict) -> None:
    res = run(INSTALL, env, "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    assert "DRY RUN" in res.stdout and "Audit type name: ParadigmIT VAPT" in res.stdout
    assert not any("insertOne" in c["stdin"] or "mongodump" in c["args"] for c in log(env))
    assert sorted(p.name for p in (env["backend"] / "report-templates").iterdir()) == ["v1.docx"]
    assert not list((env["backend"] / "backup").iterdir())


def test_apply_sequence_backup_before_write(env: dict) -> None:
    res = run(INSTALL, env, "--apply", "--stop-services", "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    steps = [
        c["args"][0]
        if c["args"][0] != "exec"
        else (
            "dump"
            if "mongodump" in c["args"]
            else "insert"
            if "insertOne" in c["stdin"]
            else "exec"
        )
        for c in log(env)
    ]
    assert steps.index("dump") < steps.index("stop") < steps.index("insert") < steps.index("start")
    bdir = next((env["backend"] / "backup").iterdir())
    rec = json.loads((bdir / "created.json").read_text())
    assert rec["template"] == "a" * 24 and rec["audittype"] == "b" * 24
    assert (bdir / "pwndoc.archive.gz").stat().st_size > 0
    assert (env["backend"] / "report-templates" / "ParadigmIT VAPT.docx").exists()


def test_refuses_existing_name(env: dict) -> None:
    res = run(INSTALL, env, "--apply", "--name", "v1", "--template", str(env["tpl"]))
    assert res.returncode == 2 and "already used" in res.stderr
    assert not any("insertOne" in c["stdin"] for c in log(env))


@pytest.mark.parametrize(
    "name", ["x'; db.dropDatabase(); '", "$(rm -rf /)", "../evil", 'a"b', "a\nb", "a`id`"]
)
def test_hostile_names_rejected_before_docker(env: dict, name: str) -> None:
    res = run(INSTALL, env, "--apply", "--name", name, "--template", str(env["tpl"]))
    assert res.returncode == 2 and "unsafe template name" in res.stderr
    assert log(env) == []


def test_values_are_json_encoded(env: dict) -> None:
    run(INSTALL, env, "--apply", "--name", "Odd Name-1.0", "--template", str(env["tpl"]))
    insert = next(c["stdin"] for c in log(env) if "insertOne" in c["stdin"])
    assert 'var n = "Odd Name-1.0"' in insert


def test_rollback_removes_only_created(env: dict) -> None:
    assert run(INSTALL, env, "--apply", "--template", str(env["tpl"])).returncode == 0
    for _ in range(2):  # idempotent
        res = run(ROLLBACK, env, "--apply")
        assert res.returncode == 0, res.stderr
    st = json.loads(env["state"].read_text())
    assert st["docs"] == ["c" * 24]
    assert sorted(p.name for p in (env["backend"] / "report-templates").iterdir()) == ["v1.docx"]


def test_update_keeps_ids(env: dict) -> None:
    assert run(INSTALL, env, "--apply", "--template", str(env["tpl"])).returncode == 0
    res = run(INSTALL, env, "--apply", "--update", "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    assert any("updateOne" in c["stdin"] for c in log(env))
    assert sum("insertOne" in c["stdin"] for c in log(env)) == 1


FILTER = ("src", "lib", "custom", "report-filters-custom.js")


def test_apply_installs_filter_fields_section_and_restarts(env: dict) -> None:
    res = run(INSTALL, env, "--apply", "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    dest = env["backend"].joinpath(*FILTER)
    assert "paradigmHTML" in dest.read_text()
    insert = next(c["stdin"] for c in log(env) if "insertOne" in c["stdin"])
    for label in ("Application Name", "Assessment Type", "Target URL", "Steps to Reproduce"):
        assert f'"label": "{label}"' in insert
    assert "db.customsections.insertOne" in insert and '"highlights"' in insert
    assert ["restart", "pwndoc-pwndoc-backend-1"] in [c["args"] for c in log(env)]
    rec = json.loads(
        next((env["backend"] / "backup").iterdir()).joinpath("created.json").read_text()
    )
    assert rec["filter"] == str(dest) and rec["filter_backup"] is None
    assert run(ROLLBACK, env, "--apply").returncode == 0
    assert not dest.exists(), "rollback removes the filter file it added"


def test_foreign_custom_filter_is_refused_and_untouched(env: dict) -> None:
    dest = env["backend"].joinpath(*FILTER)
    dest.write_text("module.exports = {mine: 1}")
    res = run(INSTALL, env, "--apply", "--template", str(env["tpl"]))
    assert res.returncode == 2 and "does not define paradigmHTML" in res.stderr
    assert dest.read_text() == "module.exports = {mine: 1}"
    assert not any("insertOne" in c["stdin"] for c in log(env))


FILTER_SRC = SCRIPTS / "pwndoc_custom" / "report-filters-custom.js"
OLD_FILTER = "defaultFilters.paradigmHTML = 1 // old paradigmHTML-only filter"


def test_update_refreshes_filter_backs_up_and_rolls_back(env: dict) -> None:
    assert run(INSTALL, env, "--apply", "--template", str(env["tpl"])).returncode == 0
    dest = env["backend"].joinpath(*FILTER)
    dest.write_text(OLD_FILTER)  # an older installed version (ours: defines paradigmHTML)
    installed = env["backend"] / "report-templates" / "ParadigmIT VAPT.docx"
    installed.write_bytes(b"old template")
    res = run(INSTALL, env, "--apply", "--update", "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    assert dest.read_text() == FILTER_SRC.read_text(), "update refreshes the filter file"
    assert "installed filter defines" in res.stdout
    restarts = [c for c in log(env) if c["args"] == ["restart", "pwndoc-pwndoc-backend-1"]]
    assert len(restarts) == 2  # install + update
    bdir = max((env["backend"] / "backup").iterdir())
    rec = json.loads((bdir / "updated.json").read_text())
    assert rec["kind"] == "update" and Path(rec["filter_backup"]).read_text() == OLD_FILTER
    assert Path(rec["file_backup"]).read_bytes() == b"old template"
    res = run(ROLLBACK, env, "--apply", "--backup-dir", str(bdir))
    assert res.returncode == 0, res.stderr
    assert dest.read_text() == OLD_FILTER and installed.read_bytes() == b"old template"
    st = json.loads(env["state"].read_text())
    assert "a" * 24 in st["docs"], "rolling back an update never deletes the installed ids"


def test_update_dry_run_touches_nothing(env: dict) -> None:
    assert run(INSTALL, env, "--apply", "--template", str(env["tpl"])).returncode == 0
    dest = env["backend"].joinpath(*FILTER)
    dest.write_text(OLD_FILTER)
    before = len(list((env["backend"] / "backup").iterdir()))
    res = run(INSTALL, env, "--update", "--template", str(env["tpl"]))
    assert res.returncode == 0, res.stderr
    assert "DRY RUN" in res.stdout and dest.read_text() == OLD_FILTER
    assert len(list((env["backend"] / "backup").iterdir())) == before


def test_verify_refuses_filter_without_sort(env: dict, tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import pwndoc_installer as inst

    custom = tmp_path / "b" / "src" / "lib" / "custom"
    custom.mkdir(parents=True)
    (custom / "report-filters-custom.js").write_text(OLD_FILTER)
    old = os.environ.get("PWNDOC_BACKEND_DIR")
    os.environ["PWNDOC_BACKEND_DIR"] = str(tmp_path / "b")
    try:
        with pytest.raises(inst.InstallError, match="sortFindings"):
            inst.verify_installed_filter()
    finally:
        if old is None:
            os.environ.pop("PWNDOC_BACKEND_DIR")
        else:
            os.environ["PWNDOC_BACKEND_DIR"] = old
