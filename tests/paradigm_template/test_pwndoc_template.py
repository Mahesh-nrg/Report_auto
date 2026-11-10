"""ParadigmIT PwnDoc template (deliverable 2): static tag checks + optional real-engine render.

The render test runs PwnDoc's own report generator inside the running backend container
(read-only: scratch in the container's /tmp) and is skipped without docker, the container,
soffice or the render harness.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "paradigm"
GUIDE = REPO.parent / "report_templates" / "generated" / "PWNDOC_TEMPLATE_GUIDE.md"
FILTERS = SCRIPTS / "pwndoc_custom" / "report-filters-custom.js"
RENDER_SH = SCRIPTS / "pwndoc_custom" / "run_render.sh"
CONTAINER = "pwndoc-pwndoc-backend-1"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TAG = re.compile(r"\{[^{}]*\}")
PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml$")

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from build_pwndoc_template import build_template

    out = tmp_path_factory.mktemp("pwt") / "tpl.docx"
    build_template().save(str(out))
    return out


def _roots(docx: Path) -> dict[str, etree._Element]:
    with zipfile.ZipFile(docx) as package:
        return {n: etree.fromstring(package.read(n)) for n in package.namelist() if PARTS.match(n)}


def _tags(docx: Path) -> list[str]:
    out = []
    for root in _roots(docx).values():
        for node in root.iter(f"{W}t"):
            out += TAG.findall(node.text or "")
    return out


def test_every_tag_sits_in_one_text_node(template: Path) -> None:
    for name, root in _roots(template).items():
        for node in root.iter(f"{W}t"):
            text = node.text or ""
            assert text.count("{") == text.count("}"), (name, text)
        for para in root.iter(f"{W}p"):
            joined = "".join(t.text or "" for t in para.iter(f"{W}t"))
            assert len(TAG.findall(joined)) == sum(
                len(TAG.findall(t.text or "")) for t in para.iter(f"{W}t")
            ), (name, joined)


def test_sections_are_balanced(template: Path) -> None:
    stack: list[str] = []
    for tag in _tags(template):
        body = tag[1:-1]
        if body[:1] in "#^":
            stack.append(body[1:])
        elif body[:1] == "/":
            assert stack, f"close without open: {tag}"
            assert stack.pop() == body[1:], tag
    assert not stack, stack


def test_every_tag_is_documented(template: Path) -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    missing = sorted({tag for tag in _tags(template) if tag not in guide})
    assert not missing, missing


def test_findings_loops_and_severity_rows(template: Path) -> None:
    tags = _tags(template)
    assert tags.count("{#findings | sortFindings}") == 2  # register row group + detail block
    assert "{#findings}" not in tags  # every loop is sorted (docs/FINDING_ORDER.md)
    for sev in ("Critical", "High", "Medium", "Low", "Informational", "Unknown"):
        assert tags.count(f"{{#orderBand == '{sev}'}}") == 2
    assert "{seq}" in tags
    assert not [t for t in tags if "changeID" in t or "where:" in t]


def test_counts_use_derived_bands(template: Path) -> None:
    tags = set(_tags(template))
    for bands in ("Critical", "High", "Medium", "Low", "Informational", "Critical,High"):
        assert f"{{findings | bandCount:'{bands}' | padNumber | d:'00'}}" in tags
    assert "{#findings | inBand:'Critical'}" in tags
    assert "{#findings | inBand:'Critical,High'}" in tags


def test_no_example_data_left(template: Path) -> None:
    text = "\n".join("".join(r.itertext()) for r in _roots(template).values())
    for sample in ("Example Client", "31 Aug", "Improper Implementation", "SAMPLE VAPT REPORT"):
        assert sample not in text


def test_numbering_reserves_html2ooxml_ids(template: Path) -> None:
    with zipfile.ZipFile(template) as package:
        root = etree.fromstring(package.read("word/numbering.xml"))
    nums = {
        n.get(f"{W}numId"): n.find(f"{W}abstractNumId").get(f"{W}val") for n in root.iter(f"{W}num")
    }
    formats = {
        a.get(f"{W}abstractNumId"): a.find(f".//{W}numFmt").get(f"{W}val")
        for a in root.iter(f"{W}abstractNum")
    }
    assert formats[nums["1"]] == "bullet"
    assert formats[nums["2"]] == "decimal"


def test_index_is_dirty_field_without_page_numbers(template: Path) -> None:
    root = _roots(template)["word/document.xml"]
    assert any(f.get(f"{W}dirty") == "true" for f in root.iter(f"{W}fldChar"))
    for para in root.iter(f"{W}p"):
        style = para.find(f"{W}pPr/{W}pStyle")
        if style is not None and style.get(f"{W}val") in ("TOC1", "TOC2"):
            last = [t.text for t in para.iter(f"{W}t") if t.text][-1]
            assert not last.strip().isdigit(), last


def test_filter_file_defines_paradigm_html() -> None:
    text = FILTERS.read_text(encoding="utf-8")
    assert "defaultFilters.paradigmHTML" in text
    for name in ("sortFindings", "inBand", "bandCount"):
        assert f"defaultFilters.{name}" in text
    assert "Object.assign(expressions.filters" in text


def _render_available() -> bool:
    if not (shutil.which("docker") and shutil.which("soffice") and RENDER_SH.exists()):
        return False
    probe = subprocess.run(  # noqa: S603, S607
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return probe.stdout.strip() == "true"


@pytest.mark.skipif(not _render_available(), reason="PwnDoc container / soffice not available")
@pytest.mark.parametrize("count", [0, 1])
def test_real_generator_render(template: Path, tmp_path: Path, count: int) -> None:
    from pwt_samples import sample_count

    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps(sample_count(count)), encoding="utf-8")
    out = tmp_path / "out.docx"
    subprocess.run(  # noqa: S603, S607
        ["sh", str(RENDER_SH), str(template), str(out), str(audit), "--filters", str(FILTERS)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    text = "\n".join("".join(r.itertext()) for r in _roots(out).values())
    assert "undefined" not in text
    assert not re.search(r"\{[#/^@%]?[a-z]", text), "unresolved tag"
    assert ("No vulnerabilities were identified." in text) == (count == 0)
    assert ("Finding 001" in text) == (count == 1)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_filter_parity_with_spec_vectors() -> None:
    """The JS sortFindings filter reproduces tests/data/finding_order_cases.json exactly."""
    script = REPO / "tests" / "paradigm_template" / "js" / "finding_order_parity.test.js"
    done = subprocess.run(  # noqa: S603, S607
        ["node", str(script)], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert "ok: 63 spec cases" in done.stdout
