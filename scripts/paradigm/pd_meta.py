"""Audit-level example values and boilerplate text of the ParadigmIT VAPT report (from the PDF).

Values that change per audit (client, application, dates, counts, highlights) are the keys the
PwnDoc template replaces with tags; the rest is the template's static boilerplate.
"""

from __future__ import annotations

import copy

DISCLAIMER = (
    (
        "This report was prepared by ParadigmIT Cyber for the "
        "exclusive benefit of the Client and is "
        "proprietary information. As it is an internal document, "
        "unauthorized use or reproduction of "
        "this document is prohibited. This report contains information about potential "
        "vulnerabilities of the web application and related web "
        "services and methods for exploiting "
        "them. ParadigmIT Cyber recommends that special precautions be taken to protect the "
        "confidentiality of both the documents and the information contained herein. The present "
        "applications are tested as of the stated assessment window. The results of this testing "
        "should not be taken as a guarantee of the security of the application, as the threat "
        "landscape and other factors may change over time. The "
        "testing only covers the state of the "
        "applications at the time they were tested, and ParadigmIT "
        "Cyber is not responsible for any "
        "changes or updates made to the application after the testing was completed.",
        8,
    ),
    (
        "This report cannot and does not protect against personal or "
        "business loss as the result of "
        "use of the applications or systems described. ParadigmIT "
        "Cyber Security Services offers no "
        "warranties, representations or legal certifications "
        "concerning the applications or systems "
        "it tests. All software includes defects: nothing in this "
        "document is intended to represent "
        "or warrant that security testing was complete and without error, nor does this document "
        "represent or warrant that the application tested is "
        "suitable to task, free of other defects "
        "than reported, fully compliant with any industry standards, or fully compatible with any "
        "operating system, hardware, or other application. By using "
        "this information, you agree that "
        "ParadigmIT Cyber shall be held harmless in any event.",
        6,
    ),
    (
        "This document is a genericised sample template. All hostnames (example.com), email "
        "addresses, tokens, keys and identifiers shown are "
        "placeholders for illustration only and do "
        "not reference any live system.",
        2,
    ),
)


EXAMPLE_META: dict = {
    "client": "Example Client",
    "application": "Example Web Application",
    "assessment_short": "Grey-box",
    "cover_meta": "GREY-BOX  /  WEB APPLICATION",
    "test_type": "Grey-box penetration test",
    "url": "app.example.com/login",
    "window": "31 Aug - 08 Sep 2026",
    "window_upper": "31 AUG - 08 SEP 2026",
    "total": "19",
    "priority": "04",
    "other": "15",
    "priority_lines": ("2 Critical", "2 High"),
    "priority_note": "Prioritise the four highest-rated findings.",
    "counts": {"Critical": 2, "High": 2, "Medium": 7, "Low": 6, "Informational": 2},
    "kicker_p3": "01  /  GREY-BOX PENETRATION TEST   /   EXAMPLE WEB APP",
    "exec_summary": (
        "ParadigmIT Cybersecurity assessed the Example Client web application to identify "
        "weaknesses that could permit unauthorised access or "
        "exposure of sensitive information. "
        "The findings and recommendations in this document support risk-based remediation and "
        "constitute the report of the completed grey-box assessment."
    ),
    "highlight_critical": (
        "Critical findings:  the report identifies an account-takeover path through the "
        "forgot-password process and an authentication-bypass issue in password validation."
    ),
    "highlight_high": (
        "High findings: rate limiting on password reset and an exposed encryption key require "
        "attention."
    ),
    "point_in_time": (
        "Point-in-time assessment: results reflect the application and environment during the "
        "testing window. Threats, configurations and exposure can change after testing."
    ),
    "methodology": (
        "The assessment combined automated and manual testing under ParadigmIT Cybersecurity's "
        "hybrid methodology, informed by OWASP, SANS/CWE and industry security-testing "
        "practices. The scope included the web application and its relevant interfaces and "
        "infrastructure as visible during the engagement."
    ),
    "limitations": (
        "Limitations: testing was conducted with background knowledge of application "
        "functionality. Results depend on the availability and "
        "visibility of the scoped systems "
        "and services at the time of assessment."
    ),
    "coverage_lead": (
        "Validation considered the OWASP Top 10 (2025), API security risks and related "
        "web-application weaknesses. The statuses below reflect the source assessment "
        "checklist."
    ),
    "tools_lead": (
        "Tools used depend on target environment and testing scenario; this "
        "selection is not exhaustive."
    ),
    "remediation_lead": (
        "The assessment identified 19 findings across five severity levels. The following "
        "sequence is proposed as a management follow-up to the documented technical "
        "recommendations; it is not a statement that remediation has occurred."
    ),
    "priority_1": (
        "Account takeover via forgot-password OTP handling; authentication bypass "
        "through missing password-parameter validation."
    ),
    "priority_1_action": (
        "Fix and independently retest the affected authentication and recovery flows."
    ),
    "priority_2": (
        "Missing rate limiting on forgot-password requests; exposed client-side encryption key."
    ),
    "priority_2_action": (
        "Address abuse controls and cryptographic-key exposure, then verify corrections."
    ),
    "priority_3": (
        "Remaining findings involve input, session, file-access and hardening "
        "weaknesses detailed in the report."
    ),
    "priority_3_action": (
        "Assign owners, track remediation and verify closure against the evidence."
    ),
    "register_intro": (
        "The following table summarises all findings identified during the assessment, with "
        "the affected location, parameter, associated weakness reference and severity rating."
    ),
    "findings_intro": (
        "Assessment evidence, reproduction steps, recommendations and references "
        "for all 19 findings."
    ),
    "disclaimer": DISCLAIMER,
    "credentials_lead": (
        "Security assessments are only as strong as the people and processes behind them. "
        "ParadigmIT Cybersecurity combines specialist testing, manual verification and "
        "cross-industry experience to deliver evidence-led security findings."
    ),
    "assurance_lead": "Quality, information security and operational maturity",
    "closing_line": "Thank you for the opportunity to support your security assessment.",
}


def example_meta() -> dict:
    """The example audit of the PDF (19 findings, Example Client / Example Web Application)."""
    return copy.deepcopy(EXAMPLE_META)
