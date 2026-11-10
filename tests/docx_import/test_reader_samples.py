"""The reader on the real sample reports (read-only copies; skipped when absent)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lxml")

from pwndoc_ai.docx_import.model import AuditHeader, ImportPlan  # noqa: E402
from pwndoc_ai.docx_import.reader import read_report  # noqa: E402

BRAND = Path("/tmp/layout/brand.docx")
BRANDARMY = Path("/tmp/refine_test/brandarmy.docx")
CLEAN = Path("/tmp/refine_test/clean.docx")
TRACKED = Path("/tmp/refine_test/tracked.docx")
DEMO = Path("/tmp/layout/demo.docx")
BRAND_IDS = ("001", "003", "002", "005", "006", "007", "010", "011", "012", "013")


def needs(*paths: Path):
    return pytest.mark.skipif(
        not all(p.exists() for p in paths), reason="sample report not present"
    )


def read(path: Path):
    return read_report(path.read_bytes(), file_name=path.name)


def check_strict(result) -> None:
    plan = ImportPlan(
        import_id="0123456789ab4def8123456789abcdef",
        revision=1,
        file_name="x.docx",
        file_sha256="0" * 64,
        header=AuditHeader(),
        source_header=result.header,
        findings=result.findings,
        images=result.images,
        issues=result.issues,
        stats=result.stats,
    )
    assert ImportPlan.from_json(plan.to_json()) == plan


@needs(BRAND)
@pytest.mark.parametrize("path", [BRAND, BRANDARMY, CLEAN, TRACKED])
def test_brandarmy_findings_are_deduplicated(path):
    if not path.exists():
        pytest.skip("sample report not present")
    result = read(path)
    check_strict(result)
    assert tuple(f.source_id for f in result.findings) == BRAND_IDS
    assert all(f.origin == "merged" and "merged_duplicate" in f.flags for f in result.findings)
    assert result.stats.summary_rows == 10 and result.stats.detail_tables == 10
    assert result.stats.id_gaps == ("004", "008", "009")
    assert [f.severity for f in result.findings].count("Low") == 5
    assert all(f.cvss_score == "" and f.cvss_vector == "" for f in result.findings)


@needs(BRAND)
def test_brandarmy_screenshots_are_placeholders_and_figures_in_005_are_deduped():
    result = read(BRAND)
    assert result.stats.figures_deduped == 3
    assert len(result.images) == 5 and all("placeholder" in i.flags for i in result.images)
    assert not any(i.include for i in result.images)
    finding_005 = next(f for f in result.findings if f.source_id == "005")
    assert [b.kind for b in finding_005.poc].count("image") == 3
    assert "References" not in "".join(b.html for b in finding_005.poc)


@needs(BRAND)
def test_brandarmy_layout_keeps_lists_and_breaks():
    finding = next(f for f in read(BRAND).findings if f.source_id == "011")
    html = finding.poc[0].html
    assert html.startswith("<p><strong>Steps to Reproduce:</strong></p><ol><li><p>")
    assert "<br>https://s.brandarmy.com/assests/f//thumbnail" in html
    assert "<p><strong>Impact:</strong></p><ul><li><p>" in html
    assert "<ul><li><p>" in finding.remediation


@needs(CLEAN, TRACKED)
def test_tracked_changes_read_like_the_accepted_copy():
    clean, tracked = read(CLEAN), read(TRACKED)
    assert [f.to_json() for f in clean.findings] == [f.to_json() for f in tracked.findings]
    assert clean.header == tracked.header and clean.images == tracked.images


@needs(CLEAN)
def test_clean_copy_still_pairs_the_reworded_006():
    result = read(CLEAN)
    finding = next(f for f in result.findings if f.source_id == "006")
    assert finding.origin == "merged" and len(result.findings) == 10


@needs(DEMO)
def test_demo_report_one_finding_two_real_screenshots():
    result = read(DEMO)
    check_strict(result)
    (finding,) = result.findings
    assert (finding.source_id, finding.severity, finding.cvss_score) == ("002", "High", "8.8")
    assert finding.cwe == "CWE-434, CWE-646" and len(finding.references) == 10
    assert finding.urls == ("http://192.168.0.1/user/profile",)
    assert result.stats.id_gaps == ("001",)
    assert [(i.media_type, i.width, i.height, i.include) for i in result.images] == [
        ("image/jpeg", 942, 518, True),
        ("image/jpeg", 942, 531, True),
    ]
    assert [b.kind for b in finding.poc] == ["html", "image", "html", "image", "html"]
    assert finding.poc[0].html.startswith("<ol><li><p>Authenticate")
    assert result.images[0].caption.startswith("Burp Suite request")
    assert len(result.blobs[0].data) == result.images[0].size
    assert "severity_table_mismatch" not in {i.code for i in result.issues}
