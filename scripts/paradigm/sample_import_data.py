"""Audit values of the ParadigmIT VAPT import sample (fictitious; hosts are example.com/.org).

Shared by ``build_import_sample.py`` and the importer tests, which assert these values.
"""

from __future__ import annotations

SAMPLE_META: dict = {
    "client": "Northwind Traders",
    "application": "Northwind Partner Portal",
    "assessment_short": "Grey-box",
    "cover_meta": "GREY-BOX  /  WEB APPLICATION",
    "test_type": "Grey-box penetration test",
    "url": "portal.example.org",
    "window": "03 Mar - 14 Mar 2026",
    "window_upper": "03 MAR - 14 MAR 2026",
    "total": "6",
    "priority": "02",
    "other": "4",
    "priority_lines": ("1 Critical", "1 High"),
    "priority_note": "Prioritise the two highest-rated findings.",
    "counts": {"Critical": 1, "High": 1, "Medium": 2, "Low": 1, "Informational": 1},
    "kicker_p3": "01  /  GREY-BOX PENETRATION TEST   /   NORTHWIND PORTAL",
    "exec_summary": (
        "ParadigmIT Cybersecurity assessed the Northwind Traders partner portal to identify "
        "weaknesses that could permit unauthorised access or exposure of sensitive "
        "information. The findings and recommendations in this document support risk-based "
        "remediation and constitute the report of the completed grey-box assessment."
    ),
    "highlight_critical": (
        "Critical findings:  an SQL injection in the order search exposes the order database."
    ),
    "highlight_high": "High findings: a stored cross-site scripting issue in partner notes.",
    "remediation_lead": (
        "The assessment identified 6 findings across five severity levels. The following "
        "sequence is proposed as a management follow-up to the documented technical "
        "recommendations; it is not a statement that remediation has occurred."
    ),
    "priority_1": "SQL injection in the order search endpoint.",
    "priority_1_action": "Fix with parameterised queries and retest the search API.",
    "priority_2": "Stored cross-site scripting in partner notes.",
    "priority_2_action": "Encode output and sanitise rich text, then verify.",
    "findings_intro": (
        "Assessment evidence, reproduction steps, recommendations and references for all 6 "
        "findings."
    ),
}

_CAPTION = "Representative example - portal.example.org"

SAMPLE_FINDINGS: list[dict] = [
    {
        "id": "001",
        "title": "SQL Injection in Order Search",
        "location": "Orders - search",
        "paths": ["https://portal.example.org/api/orders/search?q=1"],
        "parameter": "q",
        "cwe": "CWE-89",
        "cvss": "9.8 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)",
        "severity": "Critical",
        "description": (
            "The order search endpoint concatenates the q parameter into an SQL statement. "
            "An attacker can read every order and customer record."
        ),
        "images": [
            ("poc_001.png", "Representative example - search request with a quote"),
            ("poc_002.png", "Representative example - database error in the response"),
            ("poc_003.png", "Representative example - extracted table names"),
        ],
        "steps": [
            "Log in as any partner and open the order search.",
            "Search for 1' OR '1'='1 and capture the request.",
            "Observe that every order of every partner is returned.",
        ],
        "recommendations": [
            "Use parameterised queries for every database call.",
            "Apply least privilege to the database account.",
            "Add server-side input validation for search terms.",
        ],
        "references": (
            "https://owasp.org/Top10/A03_2021-Injection/\n"
            "https://cwe.mitre.org/data/definitions/89.html"
        ),
    },
    {
        "id": "002",
        "title": 'Stored XSS via <script> & "onerror" in Partner Notes',
        "location": "Partner notes",
        "paths": ["https://portal.example.org/partners/notes"],
        "parameter": "note_body",
        "cwe": "CWE-79",
        "cvss": "7.4",
        "severity": "High",
        "description": (
            "Notes accept <img src=x onerror=alert(1)> & <script>alert('x')</script> and "
            "render them unencoded for every partner who opens the note (a < b > c)."
        ),
        "images": [("poc_004.png", _CAPTION)],
        "steps": [
            "Create a note containing <img src=x onerror=alert(1)>.",
            "Open the note as another partner and observe the script running.",
        ],
        "recommendations": [
            "HTML-encode note content on output (& becomes &amp;).",
            "Sanitise rich text with an allow-list sanitiser.",
        ],
        "references": "https://owasp.org/www-community/attacks/xss/",
    },
    {
        "id": "003",
        "title": "Verbose Error Messages",
        "location": "API error handler",
        "paths": ["https://portal.example.org/api/invoices/0"],
        "parameter": "invoice_id",
        "cwe": "CWE-209",
        "cvss": "N/A",
        "severity": "Medium",
        "description": "Unhandled exceptions return stack traces with framework versions.",
        "images": [("poc_005.png", _CAPTION)],
        "steps": ["Request an invoice with id 0.", "Observe the stack trace in the response."],
        "recommendations": ["Return generic error messages and log details server-side."],
        "references": "https://cwe.mitre.org/data/definitions/209.html",
    },
    {
        "id": "004",
        "title": "Insecure Direct Object Reference on Invoices",
        "location": "Invoices - download",
        "paths": [
            "https://portal.example.org/api/invoices/1042/pdf",
            "https://portal.example.org/api/invoices/1043/pdf",
        ],
        "parameter": "invoice_id",
        "cwe": "CWE-639",
        "cvss": "6.5",
        "severity": "Medium",
        "description": "Changing invoice_id downloads invoices that belong to other partners.",
        "images": [("poc_006.png", _CAPTION)],
        "steps": [
            "Download one of your invoices and note its id.",
            "Change the id to a neighbouring value and repeat the request.",
        ],
        "recommendations": ["Check invoice ownership on the server for every request."],
        "references": "N/A",
    },
    {
        "id": "005",
        "title": "Missing Security Headers",
        "location": "All responses",
        "paths": ["https://portal.example.org/"],
        "parameter": "N/A",
        "cwe": "CWE-693",
        "cvss": "3.1",
        "severity": "Low",
        "description": "Responses lack Content-Security-Policy and X-Content-Type-Options.",
        "poc_text": ["The response headers were reviewed with curl -I; no screenshot was taken."],
        "images": [],
        "steps": ["Run curl -I https://portal.example.org/.", "Note the missing headers."],
        "recommendations": ["Add a restrictive Content-Security-Policy and nosniff header."],
        "references": "https://owasp.org/www-project-secure-headers/",
    },
    {
        "id": "006",
        "title": "Server Version Disclosure",
        "location": "HTTP Server header",
        "paths": ["https://portal.example.org/"],
        "parameter": "Server",
        "cwe": "CWE-200",
        "cvss": "0.0",
        "severity": "Info",
        "description": "The Server header reveals the web server name and version.",
        "images": [("poc_007.png", _CAPTION)],
        "steps": ["Send any request and read the Server response header."],
        "recommendations": ["Remove or genericise the Server header."],
        "references": "https://cwe.mitre.org/data/definitions/200.html",
    },
]
