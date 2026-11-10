"""Writer: ordering, PoC HTML and the finding payload (pure functions)."""

from __future__ import annotations

from test_writer_fakes import finding, image, png

from pwndoc_ai.docx_import.model import PocBlock
from pwndoc_ai.docx_import.writer import (
    creation_order,
    field_key,
    finding_payload,
    poc_html,
)

IMG_ID = "e" * 24
IMG_ID2 = "d" * 24


def test_creation_order_by_v31_band_score_then_plan_order():
    items = [
        finding("f001", "a", severity="Low", source_id="001"),
        finding("f002", "b", severity="", source_id="002"),
        finding("f003", "c", severity="High", cvss_score="7.5", source_id="003"),
        finding("f004", "d", severity="High", cvss_score="8.8", source_id="009"),
        finding("f005", "e", severity="Critical", source_id="010"),
        finding("f006", "f", severity="High", cvss_score="7.5", source_id="002"),
        finding("f007", "g", severity="High", include=False),
        finding("f008", "h", severity="High", source_id=""),
        finding(
            "f009", "i", severity="High", cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        ),
    ]
    order = [f.key for f in creation_order(items)]
    # unscored findings get an estimated v3.1 vector first (Part B): f005 Critical -> 9.8,
    # f008 High -> 7.5, f002 (no label) -> 5.3 Medium, f001 Low -> 3.1; ties keep the plan order
    assert order == ["f005", "f009", "f004", "f003", "f006", "f008", "f002", "f001"]
    created = {f.key: f for f in creation_order(items)}
    assert created["f005"].cvss_vector.startswith("CVSS:3.1/") and "cvss_estimated" in (
        created["f005"].flags
    )
    assert created["f002"].severity == "Medium" and created["f009"].flags == ()


def test_field_key_matches_pwndoc():
    assert field_key("Vulnerable URL") == "vulnerableurl"
    assert field_key("CWE/CVE") == "cwe_cve"
    assert field_key("CVSS Score") == "cvssscore"
    assert field_key("Sévérité") == "severite"


def test_poc_html_places_images_at_top_level_with_escaped_alt():
    data = png()
    f = finding(
        "f001",
        "XSS",
        poc=(
            PocBlock(kind="html", html="<p>Step 1</p>"),
            PocBlock(kind="image", image_key="i001"),
            PocBlock(kind="html", html="<ul><li><p>a</p></li></ul>"),
        ),
    )
    images = [
        image("i001", "f001", data, 1, caption='Login <form> & "quote"'),
        image("i002", "f001", png(seed=2), 5, caption="after", flags=("unmatched",)),
        image("i003", "f001", png(seed=3), 6, include=False),
        image("i004", "f002", png(seed=4), 2),
        image("i005", "f001", png(seed=5), 7),  # not uploaded: no id
    ]
    out = poc_html(f, images, {"i001": IMG_ID, "i002": IMG_ID2, "i003": "c" * 24, "i004": "b" * 24})
    assert out == (
        "<p>Step 1</p>"
        f'<img src="{IMG_ID}" alt="Login &lt;form&gt; &amp; &quot;quote&quot;">'
        "<ul><li><p>a</p></li></ul>"
        f'<img src="{IMG_ID2}" alt="after">'
    )


def test_poc_html_moved_image_is_not_placed_in_its_old_finding():
    f = finding("f001", "x", poc=(PocBlock(kind="image", image_key="i001"),))
    moved = image("i001", "f002", png(), 1)
    assert poc_html(f, [moved], {"i001": IMG_ID}) == ""
    g = finding("f002", "y", poc=())
    assert poc_html(g, [moved], {"i001": IMG_ID}) == f'<img src="{IMG_ID}" alt="Login">'


def custom(label, display="finding", **kw):
    return {"_id": "c" * 23 + str(len(label) % 10), "label": label, "fieldType": "input",
            "display": display, "displaySub": "", "text": [{"locale": "en", "value": "default"}], **kw}  # fmt: skip


def test_finding_payload_maps_custom_fields_and_skips_unknown():
    f = finding(
        "f001", "SQLi", severity="", cvss_score="", urls=("https://a/x", "https://a/y"),
        parameter="id", location="/login", cwe="CWE-89",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", references=("https://owasp",),
        description="<p>d</p>",
    )  # fmt: skip
    fields = [
        custom("Vulnerable URL"),
        custom("Vulnerable Parameter"),
        custom("Vulnerable Location"),
        custom("CWE/CVE"),
        custom("CVSS Score"),
        custom("Severity", fieldType="select", options=[{"locale": "en", "value": "Critical"}]),
        custom("Unrelated"),
    ]
    body = finding_payload(f, poc="<p>p</p>", custom_fields=fields, language="en")
    assert body["cvssv3"] == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert body["scope"] == "<p>https://a/x</p><p>https://a/y</p>"
    assert (
        body["status"] == 1
        and body["poc"] == "<p>p</p>"
        and body["references"] == ["https://owasp"]
    )
    assert "priority" not in body and "remediationComplexity" not in body
    texts = {c["customField"]["label"]: c["text"] for c in body["customFields"]}
    assert texts == {
        "Vulnerable URL": "https://a/x, https://a/y", "Vulnerable Parameter": "id",
        "Vulnerable Location": "/login", "CWE/CVE": "CWE-89", "CVSS Score": "9.8",
        "Severity": "Critical",
    }  # fmt: skip
    sent = body["customFields"][0]["customField"]
    assert "text" not in sent and sent["fieldType"] == "input" and sent["display"] == "finding"


def test_finding_payload_without_a_valid_vector_has_no_cvssv3():
    f = finding("f001", "x", cvss_vector="", cvss_score="5.0", severity="Medium", urls=("<b>&",))
    body = finding_payload(f, poc="", custom_fields=[custom("Severity")], language="en")
    assert "cvssv3" not in body
    assert body["scope"] == "<p>&lt;b&gt;&amp;</p>"
    assert body["customFields"][0]["text"] == "Medium"


def test_select_value_not_in_the_options_is_skipped():
    f = finding("f001", "x", severity="High")
    field = custom("Severity", fieldType="select", options=[{"locale": "en", "value": "Élevé"}])
    assert "customFields" not in finding_payload(f, poc="", custom_fields=[field], language="en")
