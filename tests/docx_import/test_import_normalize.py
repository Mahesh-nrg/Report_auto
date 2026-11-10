"""Part B of docs/FINDING_ORDER.md applied to imported findings (docx_import/normalize.py)."""

from __future__ import annotations

from pwndoc_ai.docx_import import normalize, ordering
from pwndoc_ai.docx_import.model import ImportedFinding

V98 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def _f(key: str, **kw) -> ImportedFinding:
    return ImportedFinding(key=key, title=kw.pop("title", f"T {key}"), **kw)


def test_unscored_finding_gets_an_estimated_vector_and_flag_without_mutating_input():
    raw = _f("f001", title="Reflected XSS in search", severity="Medium", cwe="CWE-79")
    done = normalize.normalize_imported(raw)
    assert raw.cvss_vector == "" and raw.flags == ()
    assert done.cvss_vector.startswith("CVSS:3.1/") and ordering.finding_score(done) is not None
    assert normalize.ESTIMATED_FLAG in done.flags
    assert done.severity == ordering.derived_severity(done)


def test_scored_finding_keeps_its_vector_and_band_wins_over_label():
    done = normalize.normalize_imported(_f("f001", severity="Low", cvss_vector=V98))
    assert done.cvss_vector == V98 and done.severity == "Critical" and done.flags == ()


def test_empty_text_fields_become_na_and_real_text_is_kept():
    raw = _f("f001", severity="High", cvss_score="7.5", description="<p>real</p>")
    done = normalize.normalize_imported(raw)
    assert done.description == "<p>real</p>"
    assert done.observation == normalize.NA_HTML and done.remediation == normalize.NA_HTML
    assert done.parameter == "N/A" and done.location == "N/A"


def test_informational_finding_scores_zero_with_cwe_na():
    done = normalize.normalize_imported(_f("f001", severity="Informational", cwe="CWE-200"))
    assert ordering.finding_score(done) == 0.0 and done.cwe == "N/A"
    assert done.severity == "Informational"


def test_plan_order_keeps_extracted_values_in_creation_order():
    items = [
        _f("f001", severity="Low", cvss_score="3.1"),
        _f("f002", severity="Critical"),
        _f("f003", severity="High", cvss_score="8.8"),
    ]
    planned = normalize.plan_order(items)
    assert [f.key for f in planned] == ["f002", "f003", "f001"]
    assert planned[0] is items[1]  # the preview keeps the raw values
    assert [f.key for f in normalize.normalize_findings(planned)] == [f.key for f in planned]


def test_a_failing_normaliser_leaves_the_finding_unchanged(monkeypatch):
    import pwndoc_ai.finding_normalize as shared

    def boom(*_a, **_k):
        raise RuntimeError("broken")

    monkeypatch.setattr(shared, "normalize_finding", boom)
    raw = _f("f001", severity="High")
    assert normalize.normalize_imported(raw) is raw
