"""One-off extractor: turn the measured PDF text dumps into data/report_content.json.

The source dumps (``_find_fields.json`` and ``_findings.json``) were produced by the design
recon from the customer PDF. The generated JSON is committed next to this script, so the Word
builder does not depend on the scratch directory. Run it again only to refresh the content:

    python make_content_json.py /tmp/paradigm/spec data/report_content.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LIST_MARK = re.compile(r"^\d+\.\s*$")
# Register row 011 sits across the page break in the PDF and the dump split it: fixed by hand
# from the page text (the PDF itself is the authority).
REGISTER_011 = {
    "loc": [
        "https://app.example.com/api/v1/records/assign (and related)",
        "Param: tenant",
        "Location: Records – assign",
    ],
    "name": "Broken Access Control",
    "cwe": "CWE-862",
    "sev": "Medium",
}
CVSS_VECTOR_NOTE = "The PDF shows CVSS base scores only (no vector strings)."


def _join_wrapped(lines: list[str]) -> str:
    """Join soft-wrapped PDF lines (each wrapped line keeps its trailing space)."""
    return re.sub(r"\s+", " ", "".join(lines)).strip()


def _numbered_items(lines: list[str]) -> list[str]:
    items: list[list[str]] = []
    for line in lines:
        if LIST_MARK.match(line):
            items.append([])
        elif items:
            items[-1].append(line)
        else:
            items.append([line])
    return [_join_wrapped(chunk) for chunk in items]


def _finding(fields: dict[str, list[str]]) -> dict[str, object]:
    return {
        "id": fields["S. No."][0],
        "title": _join_wrapped(fields["Name of Vulnerability"]),
        "location": _join_wrapped(fields["Vulnerable Location"]),
        "paths": [line.strip() for line in fields["Vulnerable Path / URL"]],
        "parameter": _join_wrapped(fields["Vulnerable Parameter"]),
        "cwe": _join_wrapped(fields["CVE / CWE"]),
        "cvss": _join_wrapped(fields["CVSS Score"]),
        "severity": _join_wrapped(fields["Severity"]),
        "description": _join_wrapped(fields["Description"]),
        "caption": _join_wrapped(fields["Proof of Concept"]),
        "steps": _numbered_items(fields["Steps to Reproduce"]),
        "recommendations": _numbered_items(fields["Recommendations"]),
        "references": _join_wrapped(fields["References"]),
    }


def _register(raw: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for key in sorted(raw):
        item = REGISTER_011 if key == "011" else raw[key]
        rows.append(
            {
                "id": key,
                "loc": [str(line).strip() for line in item["loc"]],  # type: ignore[union-attr]
                "name": re.sub(r"\s+", " ", str(item["name"])).strip(),
                "cwe": item["cwe"],
                "sev": item["sev"],
            }
        )
    return rows


def build(spec_dir: Path) -> dict[str, object]:
    fields = json.loads((spec_dir / "_find_fields.json").read_text(encoding="utf-8"))
    register = json.loads((spec_dir / "_findings.json").read_text(encoding="utf-8"))["register"]
    return {
        "note": CVSS_VECTOR_NOTE,
        "findings": [_finding(fields[key]) for key in sorted(fields)],
        "register": _register(register),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    data = build(Path(argv[1]))
    Path(argv[2]).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
