"""Merging the summary and detail copies of a finding, and dropping repeated figures."""

from __future__ import annotations

from pwndoc_ai.docx_import.dedupe import dedupe_figures, dedupe_findings, richness
from pwndoc_ai.docx_import.findings import Figure, RawFinding
from pwndoc_ai.docx_import.html import Para


def summary(ident: str, title: str, **kw) -> RawFinding:
    return RawFinding(
        position=kw.pop("position", 100), origin="summary", source_id=ident, title=title, **kw
    )


def detail(ident: str, title: str, **kw) -> RawFinding:
    return RawFinding(
        position=kw.pop("position", 2000), origin="detail", source_id=ident, title=title, **kw
    )


def test_summary_and_detail_copies_merge_and_the_richest_wins():
    rich = detail(
        "001",
        "Full Account Takeover",
        severity="Critical",
        description="<p>long text</p>",
        urls=("https://a/2", "https://a/1"),
        poc=(Para("step"),),
    )
    thin = summary("001", "Full Account Takeover", urls=("https://a/1", "https://a/3"), cwe="CWE-1")
    merged = dedupe_findings([thin, rich])
    assert len(merged) == 1
    entry = merged[0]
    assert entry.copies == 2 and entry.has_detail
    finding = entry.finding
    assert finding.origin == "detail" and finding.description == "<p>long text</p>"
    assert finding.cwe == "CWE-1"  # blank in the richest copy, filled from the other
    assert finding.urls == ("https://a/2", "https://a/1", "https://a/3")  # union, in order
    assert finding.position == 100  # first appearance


def test_folded_titles_pair_copies_without_ids():
    a = summary("", "WordPress XMLRPC User Enumeration")
    b = detail("", "WordPress XML-RPC User Enumeration", severity="Medium")
    merged = dedupe_findings([a, b])
    assert len(merged) == 1 and merged[0].finding.severity == "Medium"


def test_same_id_pairs_even_when_the_title_was_reworded():
    a = summary("006", "WordPress XMLRPC Enumeration")
    b = detail("006", "WordPress XML-RPC user enumeration via brute force", severity="Medium")
    assert len(dedupe_findings([a, b])) == 1


def test_conflicting_ids_are_never_merged():
    a = detail("004", "Cross-Site Scripting", position=1)
    b = detail("005", "Cross-Site Scripting", position=2)
    assert len(dedupe_findings([a, b])) == 2


def test_two_detail_tables_with_one_id_and_different_titles_stay_apart():
    a = detail("007", "SSRF", position=1)
    b = detail("007", "Open redirect", position=2)
    assert len(dedupe_findings([a, b])) == 2


def test_summary_only_finding_has_no_detail():
    merged = dedupe_findings([summary("004", "Stub"), detail("005", "Other")])
    assert [(m.finding.title, m.has_detail, m.copies) for m in merged] == [
        ("Stub", False, 1),
        ("Other", True, 1),
    ]


def test_order_follows_first_appearance_not_ids():
    raw = [
        summary("003", "C", position=1),
        summary("001", "A", position=2),
        detail("001", "A", position=9),
        detail("003", "C", position=8),
    ]
    assert [m.finding.source_id for m in dedupe_findings(raw)] == ["003", "001"]


def test_richness_counts_fields_then_characters():
    small = detail("1", "T", description="<p>x</p>")
    large = detail("1", "T", description="<p>much longer text</p>")
    assert richness(large) > richness(small)
    assert richness(detail("1", "T", severity="Low", cwe="C")) > richness(large)


def test_figures_of_the_dropped_copy_are_kept():
    a = summary("1", "T", poc=(Figure("rId1", 1),))
    b = detail(
        "1", "T", description="<p>long</p>", severity="High", poc=(Para("text"), Figure("rId2", 2))
    )
    finding = dedupe_findings([a, b])[0].finding
    assert [f.rid for f in finding.poc if isinstance(f, Figure)] == ["rId2"]
    assert [f.rid for f in finding.figures] == ["rId1"]


def test_repeated_figures_and_their_orphan_text_are_dropped():
    sha = {"r1": "s", "r2": "s", "r3": "s", "r4": "s", "r5": "t"}.get
    items = [
        Para("Steps"),
        Figure("r1", 1, "Example 1", True),
        Figure("r2", 2, "Example 2", True),
        Para("\n\nReferences:\n- "),
        Figure("r3", 3, "Example 1", True),
        Para("\n- "),
        Figure("r4", 4, "example  2", True),
        Figure("r5", 5, "Example 1", True),  # other bytes: kept
        Para("closing text"),
    ]
    kept, removed = dedupe_figures(items, sha)
    assert removed == 2
    assert [getattr(i, "rid", None) or i.text for i in kept] == [
        "Steps",
        "r1",
        "r2",
        "r5",
        "closing text",
    ]


def test_unknown_bytes_are_never_deduped():
    items = [Figure("x", 1, "a", True), Figure("y", 2, "a", True)]
    kept, removed = dedupe_figures(items, lambda rid: "")
    assert removed == 0 and len(kept) == 2


# -- closer review (COR-6) -----------------------------------------------------------------------
def test_ids_compare_as_numbers():
    merged = dedupe_findings([summary("001", "Stored XSS"), detail("1", "Stored XSS.")])
    assert len(merged) == 1 and merged[0].copies == 2


def test_different_numbers_still_never_merge():
    assert len(dedupe_findings([summary("002", "Stored XSS"), detail("3", "Stored XSS")])) == 2


def test_locations_and_parameters_are_united_and_a_severity_conflict_is_flagged():
    thin = summary("1", "IDOR", location="Admin panel", parameter="id", severity="Low")
    rich = detail(
        "1", "IDOR", location="Profile page", parameter="user_id", severity="High",
        description="<p>long text</p>",
    )  # fmt: skip
    (entry,) = dedupe_findings([thin, rich])
    assert entry.finding.location == "Profile page, Admin panel"
    assert entry.finding.parameter == "user_id, id"
    assert entry.finding.severity == "High" and entry.disagree


def test_equal_values_are_neither_repeated_nor_flagged():
    thin = summary("1", "IDOR", location="Profile  page", severity="high")
    rich = detail("1", "IDOR", location="profile page", severity="High", description="<p>x</p>")
    (entry,) = dedupe_findings([thin, rich])
    assert entry.finding.location == "profile page" and not entry.disagree
