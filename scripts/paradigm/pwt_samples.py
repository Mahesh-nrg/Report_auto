"""Sample PwnDoc audits for render-testing the ParadigmIT template (example data only).

    python pwt_samples.py OUT_DIR  -> s19 s1 s0 s60 smissing sshuffled (.json)

Each audit is shaped like PwnDoc's populated Audit.getAudit() output, the input of
/tmp/paradigm/render/render_template.js (``_images`` = image id -> data URI). The 19-finding
audit carries the PDF's own content (data/report_content.json) and evidence images.
"""

from __future__ import annotations

import base64
import copy
import html
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
CONTENT = HERE / "data" / "report_content.json"
LONG_HTML = (
    "<p>Long description with <strong>bold</strong>, <em>italic</em>, <u>underline</u> and a "
    '<a href="https://example.com/a?b=1&amp;c=2">link</a>. Unicode: Ünïcødé — “quotes” '
    "日本語 ✓. XSS-looking text: &lt;script&gt;alert(1)&lt;/script&gt; and {braces} "
    "and {#notatag}.</p><ul><li><p>bullet one</p></li><li>bullet without p</li></ul>"
    "<ol><li><p>first</p></li><li><p>second</p></li></ol>"
    "<pre><code>GET /api/v1/items?id=1' OR '1'='1 HTTP/1.1\nHost: app.example.com</code></pre>"
    "<table><tr><td>cell</td></tr></table>"
    "<p>" + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 12 + "</p>"
)


PDF_POC_PX = 475  # 356 pt at PwnDoc's 96 px/in: the evidence width of the source PDF


def _uri(path: Path, width: int | None = None) -> str:
    data = path.read_bytes()
    if width:
        data = _scaled(path, width) or data
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _scaled(path: Path, width: int) -> bytes | None:
    """PNG scaled to ``width`` px with Pillow when available (analysis venv), else None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(path) as img:
        height = round(img.height * width / img.width)
        buf = io.BytesIO()
        img.resize((width, height), Image.LANCZOS).save(buf, "PNG")
        return buf.getvalue()


def _field(label: str, text: str, kind: str = "input") -> dict:
    return {"customField": {"label": label, "fieldType": kind, "display": "finding"}, "text": text}


def _ol(items: list[str]) -> str:
    return "<ol>" + "".join(f"<li><p>{html.escape(t)}</p></li>" for t in items) + "</ol>"


def _finding(item: dict, image_id: str) -> dict:
    caption = item["caption"].replace("—", "-")
    return {
        "identifier": int(item["id"]),
        "title": item["title"],
        "vulnType": "Web Application",
        "description": f"<p>{html.escape(item['description'])}</p>",
        "observation": "",
        "remediation": _ol(item["recommendations"]),
        "poc": f'<p></p><img src="{image_id}" alt="{caption}">',
        "references": [] if item["references"] in ("N/A", "-") else [item["references"]],
        "scope": "",
        "status": 0,
        "category": "Web Application",
        "cvssv3": "",
        "retestDescription": "",
        "customFields": [
            _field("Vulnerable URL", " ".join(item["paths"])),
            _field("Vulnerable Parameter", item["parameter"]),
            _field("Vulnerablelocation", item["location"]),
            _field("CWEcve", item["cwe"]),
            _field("CVSSscore", item["cvss"]),
            _field("Severity", item["severity"]),
            _field("Steps to Reproduce", _ol(item["steps"]), "text"),
        ],
    }


def _base() -> dict:
    return {
        "name": "Example Web Application - VAPT",
        "auditType": "ParadigmIT VAPT",
        "language": "en",
        "date": "2026-09-10",
        "date_start": "2026-08-31",
        "date_end": "2026-09-08",
        "company": {
            "name": "Example Client",
            "shortName": "Example",
            "logo": _uri(ASSETS / "logo_header.png"),
        },
        "client": {
            "firstname": "Example",
            "lastname": "Client",
            "email": "client@example.com",
            "phone": "+1 555 0100",
            "cell": "",
            "title": "CISO",
        },
        "creator": {
            "username": "tester",
            "firstname": "Alex",
            "lastname": "Tester",
            "email": "alex@example.com",
            "phone": "",
            "role": "user",
        },
        "collaborators": [],
        "scope": [{"name": "app.example.com/login", "hosts": []}],
        "customFields": [
            {
                "customField": {"label": "Application Name", "fieldType": "input"},
                "text": "Example Web Application",
            },
            {"customField": {"label": "Assessment Type", "fieldType": "input"}, "text": "Grey-box"},
            {
                "customField": {"label": "Target URL", "fieldType": "input"},
                "text": "app.example.com/login",
            },
        ],
        "sections": [
            {
                "field": "highlights",
                "name": "Assessment highlights",
                "text": (
                    "<p>Critical findings:  the report identifies an account-takeover path "
                    "through the forgot-password process and an authentication-bypass issue in "
                    "password validation.</p><p>High findings: rate limiting on password reset "
                    "and an exposed encryption key require attention.</p>"
                ),
            }
        ],
        "findings": [],
        "_images": {},
    }


def sample19() -> dict:
    audit = _base()
    for item in json.loads(CONTENT.read_text(encoding="utf-8"))["findings"]:
        image_id = f"6a00000000000000000000{int(item['id']):02d}"
        audit["_images"][image_id] = _uri(ASSETS / f"poc_{item['id']}.png", PDF_POC_PX)
        audit["findings"].append(_finding(item, image_id))
    return audit


def sample_count(n: int) -> dict:
    full = sample19()
    audit = copy.deepcopy(full)
    audit["findings"] = []
    for index in range(n):
        finding = copy.deepcopy(full["findings"][index % 19])
        finding["identifier"] = index + 1
        if n > 19:
            finding["description"] = LONG_HTML
        audit["findings"].append(finding)
    if n == 0:
        audit["_images"] = {}
    return audit


def sample_missing() -> dict:
    """Optional data absent: no custom fields/sections/dates/scope, empty rich fields."""
    audit = sample_count(3)
    audit.update(date_start="", date_end="", scope=[], sections=[], customFields=[])
    audit["company"] = {"name": "Example Client"}
    for index, finding in enumerate(audit["findings"]):
        finding["customFields"] = [] if index == 0 else finding["customFields"][:2]
        finding.update(description="", poc="", remediation="", references=[])
    audit["findings"][2]["customFields"].append(_field("Severity", "Unrated"))
    return audit


SHUFFLE_SEED = 20261026
# (source id, cvssv3, CVSSscore, Severity): mixed labels / vectors / missing scores
SHUFFLE_EDITS = (
    ("012", "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "", "Low"),  # 9.8, label says Low
    ("005", "", "7,5", "high"),  # comma decimal, lower-case label
    ("009", "", "", "Moderate"),  # no score: label alias decides, after the scored Mediums
    ("016", "", "N/A", ""),  # nothing usable: Unknown band, listed last
    ("003", "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", "2.0", "Critical"),  # 7.5 wins
    ("018", "", "0", "Info"),  # informational, CWE forced to N/A
)


def _set_field(finding: dict, label: str, text: str) -> None:
    fields = [f for f in finding["customFields"] if f["customField"]["label"] != label]
    finding["customFields"] = [*fields, _field(label, text)]


def sample_shuffled() -> dict:
    """The 19 findings stored in a shuffled order with mixed labels, vectors and scores."""
    import random

    audit = sample19()
    edits = {e[0]: e[1:] for e in SHUFFLE_EDITS}
    for finding in audit["findings"]:
        key = f"{finding['identifier']:03d}"
        if key in edits:
            vector, score, label = edits[key]
            finding["cvssv3"] = vector
            _set_field(finding, "CVSSscore", score)
            _set_field(finding, "Severity", label)
    random.Random(SHUFFLE_SEED).shuffle(audit["findings"])  # noqa: S311 - test data
    return audit


def main(argv: list[str]) -> int:
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    samples = {
        "s19": sample19(),
        "s1": sample_count(1),
        "s0": sample_count(0),
        "s60": sample_count(60),
        "smissing": sample_missing(),
        "sshuffled": sample_shuffled(),
    }
    for name, audit in samples.items():
        (out / f"{name}.json").write_text(json.dumps(audit), encoding="utf-8")
        print(out / f"{name}.json", len(audit["findings"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
