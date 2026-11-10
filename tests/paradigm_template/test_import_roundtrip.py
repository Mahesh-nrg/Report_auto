"""The ParadigmIT import sample through the real DOCX importer: rebuilt from its generator,
read field by field against the values it was built from, and imported end to end through the
real import routes into the in-process PwnDoc simulator (create, then undo to nothing).
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("lxml")
pytest.importorskip("docx")

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "paradigm"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

from build_import_sample import sample_content  # noqa: E402
from sample_import_data import SAMPLE_META  # noqa: E402
from test_import_samples import (  # noqa: E402, F401 - rig and sim are fixtures
    IMG,
    analysed,
    assert_allow_list,
    check_pwndoc,
    created,
    rig,
    sim,
    undone,
)

from pwndoc_ai.docx_import.reader import read_report  # noqa: E402

# The sample lists its findings in report order (CVSS v3.1 desc, renumbered 001..): the 6.5
# Medium precedes the unscored Medium (docs/FINDING_ORDER.md part A).
SAMPLE_FINDINGS = sample_content()["findings"]
SEVERITY = {"Info": "Informational"}
TAGS = re.compile(r"<[^>]+>")


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> Path:
    from build_import_sample import build_sample

    out = tmp_path_factory.mktemp("paradigm") / "ParadigmIT_VAPT_Import_Sample.docx"
    build_sample().save(str(out))
    return out


@pytest.fixture(scope="module")
def report(sample):
    return read_report(sample.read_bytes(), file_name=sample.name)


def _text(value: str) -> str:
    return html.unescape(TAGS.sub("", value))


def _score(printed: str) -> str:
    """The score as the reader keeps it: the first number, trailing ``.0`` dropped."""
    first = printed.split()[0]
    return "" if first == "N/A" else first.removesuffix(".0")


def _blank(value: str) -> str:
    return "" if value == "N/A" else value


def test_sample_is_in_cvss_order() -> None:
    assert [f["id"] for f in SAMPLE_FINDINGS] == ["001", "002", "003", "004", "005", "006"]
    assert [f["src_id"] for f in SAMPLE_FINDINGS] == ["001", "002", "004", "003", "005", "006"]


def test_every_finding_once_in_order(report):
    assert [f.source_id for f in report.findings] == [f["id"] for f in SAMPLE_FINDINGS]
    assert all(f.origin == "merged" for f in report.findings), "register + detail deduped"
    assert report.stats.summary_rows == 6 and report.stats.detail_tables == 6


@pytest.mark.parametrize("index", range(len(SAMPLE_FINDINGS)))
def test_every_field(report, index):
    want, got = SAMPLE_FINDINGS[index], report.findings[index]
    assert got.title == want["title"]
    assert got.location == want["location"] and got.parameter == _blank(want["parameter"])
    assert got.urls == tuple(want["paths"])
    assert got.cwe == want["cwe"]
    assert got.severity == SEVERITY.get(want["severity"], want["severity"])
    assert got.cvss_score == _score(want["cvss"])
    assert _text(got.description) == want["description"]
    refs = () if want["references"] == "N/A" else tuple(want["references"].split("\n"))
    assert got.references == refs
    assert got.remediation.startswith("<ol><li>")
    assert got.remediation.count("<li>") == len(want["recommendations"])
    poc = "".join(b.html for b in got.poc if b.kind == "html")
    assert "<strong>Steps to reproduce:</strong><" in poc
    assert poc.count("<li>") == len(want["steps"])
    for block in got.poc:
        if block.kind == "html":
            assert_allow_list(block.html)


def test_screenshots_follow_their_captions(report):
    for want, got in zip(SAMPLE_FINDINGS, report.findings, strict=True):
        keys = [b.image_key for b in got.poc if b.kind == "image"]
        images = [i for i in report.images if i.finding_key == got.key]
        assert [i.key for i in images] == keys
        assert [i.caption for i in images] == [c for _name, c in want["images"]]
        assert all(i.include and not i.flags for i in images)
    assert len(report.images) == sum(len(f["images"]) for f in SAMPLE_FINDINGS)


def test_html_special_characters_survive(report):
    xss = report.findings[1]
    assert xss.title == 'Stored XSS via <script> & "onerror" in Partner Notes'
    assert "&lt;script&gt;" in xss.description and "<script" not in xss.description


def test_header_and_reader_issues(report):
    assert report.header.company == SAMPLE_META["client"]
    assert report.header.date_start == "2026-03-03" and report.header.date_end == "2026-03-14"
    assert {i.code for i in report.issues} == {"duplicates_merged", "contacts_absent"}


def test_routes_import_and_undo(rig, sim, sample):  # noqa: F811 - fixtures
    import_id, plan = analysed(rig, sample)
    assert plan["header"]["company"] == "Northwind Traders"
    assert plan["header"]["date_start"] == "2026-03-03"
    missing = {i["finding_key"] for i in plan["issues"] if i["code"] == "cvss_missing"}
    assert "f004" in missing and "f001" not in missing, "only printed vectors count"
    images = sum(len(f["images"]) for f in SAMPLE_FINDINGS)
    status = created(rig, import_id, plan)
    assert status["result"]["images_uploaded"] == images
    audit = check_pwndoc(sim, plan, "Northwind Traders", len(SAMPLE_FINDINGS), images)
    first = audit["findings"][0]
    assert len(IMG.findall(first["poc"])) == 3
    assert first["poc"].index("<img") < first["poc"].index("Steps to reproduce")
    result = undone(rig, import_id)["undo"]
    assert {item["outcome"] for item in result["items"]} == {"deleted"}
    assert sim.is_empty()
