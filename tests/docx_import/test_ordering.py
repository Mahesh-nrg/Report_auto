"""Importer finding order (docs/FINDING_ORDER.md Part A): shared vectors, plan, edits, writer,
completeness and the verification report."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest
from plan_fakes import (
    build,
    codes,
    finding,
    report,
    text_tools,  # noqa: F401 - autouse fixture
)

from pwndoc_ai.docx_import import ordering, plan, verify, writer
from pwndoc_ai.docx_import.model import PlanEdits
from pwndoc_ai.docx_import.normalize import normalize_findings

DATA = Path(__file__).resolve().parents[1] / "data"
GENERATED = Path("/mnt/data/Pentesting/Report_automation/report_templates/generated")
SAMPLES = [
    GENERATED / "ParadigmIT_VAPT_Import_Sample.docx",
    GENERATED / "ParadigmIT_VAPT_Report.docx",
]
V98 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
V53 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N"
V00 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"
V4 = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"


def _cases() -> list[dict]:
    return json.loads((DATA / "finding_order_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_order_rows_reproduce_the_shared_vectors(case: dict) -> None:
    rows = ordering.order_rows(case["findings"])
    got = [{k: r[k] for k in ("title", "score", "band", "seq", "mismatch")} for r in rows]
    assert got == case["expected"]


def _plain(i: int, **values: object) -> object:
    base = {"title": f"F{i:02d}", "severity": "", "cvss_vector": "", "cvss_score": ""}
    base.update(values)
    return finding(f"f{i:03d}", **base)


def _nineteen() -> list:
    """19 findings covering every band, scored/unscored, v4, mismatching labels and ties."""
    specs = [
        {"cvss_vector": V98, "severity": "Low"},
        {"cvss_score": "9.1", "severity": "Critical"},
        {"severity": "Critical"},
        {"cvss_score": "8.8", "severity": "High"},
        {"cvss_score": "7.5", "severity": "High"},
        {"cvss_score": "7.5", "severity": "Medium"},
        {"severity": "High"},
        {"cvss_vector": V53, "severity": "Medium"},
        {"cvss_score": "6.1", "severity": "Medium"},
        {"cvss_score": "6.5", "severity": "High"},
        {"cvss_score": "4.0", "severity": "Low"},
        {"cvss_score": "3.1", "severity": "Low"},
        {"cvss_score": "2.0", "severity": "Informational"},
        {"severity": "Low"},
        {"cvss_vector": V00, "severity": "Informational"},
        {"cvss_score": "0", "severity": "Informational"},
        {"severity": "Informational"},
        {"cvss_score": "", "severity": ""},
        {"cvss_score": "10", "severity": "Critical"},
    ]
    return [_plain(i + 1, **spec) for i, spec in enumerate(specs)]


def test_nineteen_shuffled_findings_build_the_same_plan_order() -> None:
    items = _nineteen()
    expected = [f.key for f in ordering.sort_findings(items)]
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    built = build(report(findings=tuple(shuffled), images=()))
    keys = [f.key for f in built.findings]
    by_score = {f.key: ordering.finding_score(f) for f in items}
    # f003 (Critical, no score) is estimated 9.8 (Part B) and ties with f001 in plan order
    assert keys[0] == "f019" and set(keys[1:3]) == {"f001", "f003"}  # 10.0, 9.8, 9.8 (est.)
    assert keys[3] == "f002"  # 9.1
    assert set(keys) == set(expected) and len(keys) == 19
    created = normalize_findings(built.findings)  # what the writer creates, in plan order
    assert [f.key for f in created] == keys
    bands = [ordering.derived_severity(f) for f in created]
    ranks = [ordering.BANDS.index(b or "Unknown") for b in bands]
    assert ranks == sorted(ranks) and bands[-1] == "Informational"
    f018 = next(f for f in created if f.key == "f018")  # no label, no score: estimated
    assert ordering.derived_severity(f018) == "Medium" and "cvss_estimated" in f018.flags
    assert by_score["f010"] == 6.5 and ordering.derived_severity(items[9]) == "Medium"
    assert ordering.score_from(V4, "9.3") is None  # a v4 vector blocks its own number
    report_ = verify.verify_plan(built)
    assert report_["checks"]["order_monotonic"] == "pass"
    assert [e["seq"] for e in report_["findings"]] == [f"{n:03d}" for n in range(1, 20)]


def test_sorting_is_stable_and_idempotent() -> None:
    items = _nineteen()
    once = ordering.sort_findings(items)
    assert ordering.sort_findings(once) == once
    ties = [_plain(1, cvss_score="7.5"), _plain(2, cvss_score="7.5"), _plain(3, cvss_score="7.5")]
    assert [f.key for f in ordering.sort_findings(ties)] == ["f001", "f002", "f003"]


def test_excluded_findings_stay_in_the_plan_but_get_no_number() -> None:
    items = [_plain(1, cvss_score="5"), _plain(2, cvss_score="9", include=False)]
    assert [f.key for f in ordering.sort_findings(items)] == ["f002", "f001"]
    assert ordering.numbers(items) == {"f001": "001"}
    assert [f.key for f in writer.creation_order(items)] == ["f001"]


def test_editing_cvss_in_the_preview_recomputes_the_order_server_side() -> None:
    items = (_plain(1, cvss_score="9.0"), _plain(2, cvss_score="3.0"))
    built = build(report(findings=items, images=()))
    assert [f.key for f in built.findings] == ["f001", "f002"]
    edits = PlanEdits.from_json(
        {"revision": 1, "findings": [{"key": "f002", "cvss_vector": V98, "cvss_score": ""}]}
    )
    changed = plan.apply_edits(built, edits, clean=lambda value: value)
    assert [f.key for f in changed.findings] == ["f002", "f001"]
    assert [f.key for f in built.findings] == ["f001", "f002"]  # not mutated


def test_label_mismatch_is_a_warning_and_the_writer_uses_the_band() -> None:
    wrong = _plain(1, cvss_vector=V98, cvss_score="9.8", severity="Low")
    built = build(report(findings=(wrong,), images=()))
    assert "severity_label_mismatch" in codes(built.issues)
    assert built.findings[0].severity == "Low"  # the label is kept, the band is written
    values = writer.custom_values(built.findings[0])
    assert values["severity"] == "Critical" and values["cvssscore"] == "9.8"
    entry = verify.verify_plan(built)["findings"][0]
    assert (
        entry["checks"]["severity_matches_band"] == "fixed" and entry["note"] == "Low -> Critical"
    )


def test_no_score_keeps_the_label_with_the_cvss_missing_warning() -> None:
    built = build(report(findings=(_plain(1, severity="High"),), images=()))
    assert "cvss_missing" in codes(built.issues)
    assert "severity_label_mismatch" not in codes(built.issues)
    assert writer.custom_values(built.findings[0])["severity"] == "High"


def test_vector_wins_over_a_disagreeing_printed_score() -> None:
    item = _plain(1, cvss_vector=V53, cvss_score="9.9", severity="Medium")
    assert writer.score_of(item) == 5.3
    assert writer.custom_values(item)["cvssscore"] == "5.3"


def test_verify_plan_flags_duplicates_and_audit_order() -> None:
    items = (_plain(1, title="Same", cvss_score="5"), _plain(2, title="same ", cvss_score="4"))
    built = build(report(findings=items, images=()))
    result = verify.verify_plan(built)
    assert result["checks"]["duplicate_titles"] == ["same"] and not result["ok"]


def _pwndoc(title: str, vector: str, score: str, severity: str) -> dict:
    fields = [
        {"customField": {"label": "CVSSscore"}, "text": score},
        {"customField": {"label": "Severity"}, "text": severity},
    ]
    return {"title": title, "cvssv3": vector, "customFields": fields}


def test_verify_audit_reads_pwndoc_findings_in_stored_order() -> None:
    good = [_pwndoc("A", V98, "9.8", "Critical"), _pwndoc("B", V53, "5.3", "Medium")]
    result = verify.verify_audit(good)
    assert result["ok"] and [e["seq"] for e in result["findings"]] == ["001", "002"]
    bad = verify.verify_audit([good[1], _pwndoc("C", V98, "7.0", "Low")])
    assert not bad["ok"] and bad["checks"]["order_monotonic"] == "fail"
    checks = bad["findings"][1]["checks"]
    assert checks["score_matches_vector"] == "fail" and checks["severity_matches_band"] == "fail"


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.stem)
def test_paradigm_sample_reimport_is_stable_when_shuffled(sample: Path) -> None:
    pytest.importorskip("lxml")
    if not sample.is_file():
        pytest.skip("the ParadigmIT import sample is not present")
    from pwndoc_ai.docx_import.reader import read_report

    read = read_report(sample.read_bytes(), file_name=sample.name)
    first = ordering.sort_findings(read.findings)
    shuffled = list(read.findings)
    random.Random(3).shuffle(shuffled)
    assert [f.key for f in first] == [f.key for f in read.findings]  # already in order
    again = ordering.sort_findings([replace(f) for f in shuffled])

    def keys(items):  # equal scores are ties: they keep the (shuffled) input order
        return [(ordering.derived_severity(f), ordering.finding_score(f)) for f in items]

    assert keys(again) == keys(first)
    bands = [ordering.derived_severity(f) for f in first]
    assert bands == sorted(bands, key=lambda b: ordering.BANDS.index(b or "Unknown"))
