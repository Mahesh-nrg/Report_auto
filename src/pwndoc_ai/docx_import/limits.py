"""Limits, id patterns and enumerations shared by every importer module.

Builders read caps as ``limits.NAME`` at call time (``from . import limits``) so tests can
monkeypatch them; the strict JSON validation in ``model``/``records`` binds them at import.
"""

from __future__ import annotations

import re

KIB = 1024
MIB = 1024 * KIB

# Limits (read as model.NAME at call time so tests can monkeypatch them).
MAX_FINDINGS = 200
MAX_IMAGES = 200
MAX_IMAGE_BYTES = 8 * MIB
MAX_TOTAL_IMAGE_BYTES = 64 * MIB
PLACEHOLDER_BYTES = 200  # smaller images (or 1x1 px) are flagged "placeholder"
MAX_IMAGE_PIXELS = 20_000  # per side
MAX_NAME_CHARS = 200  # company, audit name, audit type, contact names
MAX_TITLE_CHARS = 300
MAX_LINE_CHARS = 2000  # one-line plain fields: parameter, location, url, cwe
MAX_RICH_CHARS = 50_000  # one HTML field or one PoC html block
MAX_POC_BLOCKS = 400
MAX_POC_CHARS = 200_000
MAX_URLS = 100
MAX_REFERENCES = 50
MAX_REFERENCE_CHARS = 1000
MAX_SCOPE_ITEMS = 100
MAX_SCOPE_CHARS = 500
MAX_CAPTION_CHARS = 300
MAX_ISSUES = 1000
MAX_HEADER_TEXT_CHARS = 8000
MAX_OPTIONS = 500
MAX_CANDIDATES = 10
MAX_FILE_NAME_CHARS = 255
MAX_EMAIL_CHARS = 254
MAX_ERROR_CHARS = 200
MAX_CURRENT_CHARS = 200
MAX_REVISION = 1_000_000
MAX_COUNT = 1_000_000
MAX_JOURNAL_OBJECTS = MAX_FINDINGS + MAX_IMAGES + 8
MAX_IMPORTS_PER_OWNER = 20
MAX_OWNER_IMPORT_BYTES = 512 * MIB
MAX_RUNNING_PER_USER = 1
MAX_RUNNING_TOTAL = 2

IMPORT_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")  # uuid4().hex, = FILE_ID
OBJECT_ID = re.compile(r"[0-9a-f]{24}")  # a PwnDoc ObjectId (also passes SAFE_ID)
FINDING_KEY = re.compile(r"f\d{3,4}")
IMAGE_KEY = re.compile(r"i\d{3,4}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_ID = re.compile(r"\d{1,6}")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
CVSS_SCORE = re.compile(r"10(?:\.0)?|\d(?:\.\d)?")
CVSS_VECTOR = re.compile(r"CVSS:3\.[01](?:/[A-Z]{1,3}:[A-Z]){1,30}")
LOCALE = re.compile(r"[\w \[\]'(),-]{1,32}")  # PwnDoc's validFilename, as used for language
EMAIL = re.compile(r"[^\s@<>\"']{1,64}@[^\s@<>\"']{1,189}")
CONTROL_LINE = re.compile(r"[\x00-\x1f\x7f]")
CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SEVERITIES = ("Critical", "High", "Medium", "Low", "Informational")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif")
IMAGE_FLAGS = frozenset(
    {"placeholder", "unsupported", "too_large", "uncaptioned", "unmatched", "duplicate_caption"}
)
UNUSABLE_IMAGE_FLAGS = frozenset({"unsupported", "too_large"})
FINDING_ORIGINS = ("detail", "summary", "merged")
FINDING_FLAGS = frozenset(
    {
        "summary_only",
        "truncated",
        "merged_duplicate",
        "copies_disagree",
        "description_split",
        "cvss_estimated",
    }
)
LLM_FIELDS = frozenset({"title", "severity", "description", "observation"})
FINDING_FIELDS = (
    "title", "severity", "cvss_vector", "cvss_score", "cwe", "urls", "parameter", "location",
    "description", "observation", "poc", "remediation", "references", "include",
)  # fmt: skip
HEADER_FIELDS = (
    "company", "audit_name", "language", "audit_type", "template_id", "date_start",
    "date_end", "scope", "contact",
)  # fmt: skip
IMAGE_EDIT_FIELDS = ("caption", "finding_key", "include")
ISSUE_FIELDS = frozenset(FINDING_FIELDS) | frozenset(HEADER_FIELDS) | {"images"}

IMPORT_STATES = (
    "analysing", "ready", "analysis_failed", "cancelled", "creating", "created",
    "create_failed", "undoing", "undone", "undo_failed", "interrupted",
)  # fmt: skip
PHASES = ("", "analyse", "create", "undo")
JOURNAL_STATES = ("open", "complete", "rolled_back", "partial", "undone")
OBJECT_KINDS = ("company", "client", "audit", "image", "finding")
UNDO_OUTCOMES = ("deleted", "gone", "kept", "failed")
ISSUE_LEVELS = ("blocker", "warning", "info")

# code -> (level, fixed user-facing sentence). Never put document text in these.
ISSUES: dict[str, tuple[str, str]] = {
    "company_missing": ("blocker", "Enter the company name."),
    "audit_name_missing": ("blocker", "Enter the audit name."),
    "language_missing": ("blocker", "Choose the audit language."),
    "audit_type_missing": ("blocker", "Choose the audit type."),
    "template_missing": ("blocker", "Choose the report template."),
    "no_findings": ("blocker", "Include at least one finding."),
    "title_missing": ("blocker", "A finding has no title."),
    "date_invalid": ("blocker", "A date is not a valid YYYY-MM-DD date."),
    "date_order": ("blocker", "The start date is after the end date."),
    "dates_missing": ("warning", "The report has no start or end date."),
    "scope_missing": ("warning", "The report lists no scope."),
    "severity_missing": (
        "warning",
        "A finding has no severity; the PwnDoc report leaves it out until one is set.",
    ),
    "cvss_missing": (
        "warning",
        "A finding has no CVSS vector; unscored, it goes after the scored ones of its severity.",
    ),
    "fields_blank": ("warning", "Some fields of a finding are blank in the document."),
    "summary_only": ("warning", "A finding appears only in the summary table, not in the details."),
    "id_gaps": (
        "warning",
        "Finding numbers are missing from the document; findings may be absent.",
    ),
    "image_unmatched": (
        "warning",
        "A screenshot had no finding; it was put after the nearest one.",
    ),
    "image_placeholder": ("warning", "A screenshot looks like a placeholder; it is left out."),
    "image_unsupported": (
        "warning",
        "A screenshot has a format PwnDoc cannot show; it is left out.",
    ),
    "image_too_large": ("warning", "A screenshot is too large to upload; it is left out."),
    "image_uncaptioned": ("warning", "A screenshot has no caption."),
    "duplicate_audit": ("warning", "An audit with this name already exists for this company."),
    "previous_import": ("warning", "This file was already imported into PwnDoc before."),
    "leftover_name": (
        "warning",
        "The document mentions another company in its body text, probably a template leftover.",
    ),
    "company_conflict": ("warning", "The document names more than one company; check the name."),
    "severity_table_mismatch": (
        "warning",
        "The severity summary table disagrees with the findings; the findings are used.",
    ),
    "severity_label_mismatch": (
        "warning",
        "The severity label disagrees with the CVSS v3.1 score; the band of the score is used.",
    ),
    "llm_fields": ("warning", "Some fields were filled by the AI model; check them."),
    "text_truncated": ("warning", "A field was longer than allowed and was shortened."),
    "contacts_absent": ("info", "This template carries no contacts; add one if needed."),
    "duplicates_merged": ("info", "Findings listed twice were merged into one."),
    "copies_disagree": (
        "warning",
        "The two copies of a finding disagree on severity, CWE or CVSS; check the kept values.",
    ),
    "figures_deduped": ("info", "Repeated screenshots were removed."),
    "company_existing": ("info", "The company already exists in PwnDoc and will be reused."),
    "company_new": ("info", "The company will be created in PwnDoc."),
    "llm_unavailable": ("info", "No AI model is configured; only the document reader was used."),
    "llm_failed": ("info", "The AI model failed; the fields it should fill are left blank."),
    "llm_skipped_unmasked": (
        "info",
        "The AI model was not asked about the findings: no company name is known to mask.",
    ),
}
