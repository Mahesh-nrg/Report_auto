"""Finding order of the ParadigmIT deliverables (docs/FINDING_ORDER.md part A): the Word/import
sample ordering helper, the render checker and, with the PwnDoc container, a real render of a
shuffled audit through the sortFindings filter."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "paradigm"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pd_order import content_score, numeric_score, order_content  # noqa: E402
from pwt_order_check import check_docx, docx_view, expected_order  # noqa: E402
from pwt_samples import sample_count, sample_shuffled  # noqa: E402

FILTERS = SCRIPTS / "pwndoc_custom" / "report-filters-custom.js"
RENDER_SH = SCRIPTS / "pwndoc_custom" / "run_render.sh"
CONTAINER = "pwndoc-pwndoc-backend-1"
SHUFFLED = (
    ("Critical", "Missing Server-Side Input Length Validation"),
    ("Critical", "Authentication Bypass – Password Parameter Validation Missing"),
    ("Critical", "Improper Implementation of Password Reset Resulting in Account Takeover"),
    ("High", "Missing Rate Limiting on Password Reset Leading to Email Flooding"),
    ("High", "Exposed Encryption Key"),
    ("High", "No Rate Limit on OTP Verification"),
)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("9.8 (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
        ("7,5", 7.5),
        ("7.45", 7.5),
        ("N/A", None),
        ("-", None),
        ("10.1", None),
        ("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N", None),
    ],
)
def test_content_score(raw: str, want: float | None) -> None:
    assert content_score(raw) == want


def test_numeric_score_rejects_bool_and_nan() -> None:
    assert numeric_score(True) is None
    assert numeric_score(float("nan")) is None
    assert numeric_score(3) == 3.0


def test_order_content_sorts_renumbers_and_keeps_input() -> None:
    content = {
        "findings": [
            {"id": "001", "cvss": "4.4", "severity": "Medium", "title": "m"},
            {"id": "002", "cvss": "N/A", "severity": "Info", "title": "i"},
            {"id": "003", "cvss": "9.1", "severity": "Low", "title": "c"},
            {"id": "004", "cvss": "N/A", "severity": "Medium", "title": "m2"},
            {"id": "005", "cvss": "4.4", "severity": "Medium", "title": "m3"},
        ],
        "register": [{"id": f"00{i}", "sev": "x"} for i in range(1, 6)],
    }
    frozen = copy.deepcopy(content)
    out = order_content(content)
    assert content == frozen
    assert [f["title"] for f in out["findings"]] == ["c", "m", "m3", "m2", "i"]
    assert [f["id"] for f in out["findings"]] == ["001", "002", "003", "004", "005"]
    assert [f["src_id"] for f in out["findings"]] == ["003", "001", "005", "004", "002"]
    assert out["findings"][0]["severity"] == "Critical"  # derived band wins over 'Low'
    assert [r["id"] for r in out["register"]] == ["001", "002", "003", "004", "005"]
    assert out["register"][0]["sev"] == "Critical"


def test_word_report_example_is_in_cvss_order() -> None:
    from build_word_template import CONTENT

    out = order_content(json.loads(CONTENT.read_text(encoding="utf-8")))
    src = [f["src_id"] for f in out["findings"]]
    assert src[:5] == ["002", "001", "004", "003", "007"]
    assert src[-2:] == ["018", "019"]
    assert [f["id"] for f in out["findings"]] == [f"{i:03d}" for i in range(1, 20)]


def test_expected_order_of_shuffled_sample() -> None:
    order = expected_order(sample_shuffled())
    assert [(o["band"], o["title"]) for o in order[:6]] == list(SHUFFLED)
    assert order[-1] == {"seq": "019", "title": "Concurrent Login", "band": "Unknown"}
    assert [o["seq"] for o in order] == [f"{i:03d}" for i in range(1, 20)]


def test_check_docx_flags_wrong_order() -> None:
    expected = expected_order(sample_count(2))
    good = {
        "headings": [(e["seq"], e["title"]) for e in expected],
        "register": [(e["seq"], e["title"], e["band"], "FBE5E5") for e in expected],
        "severities": [("Critical", "FBE5E5")] * 2,
    }
    assert check_docx(expected, good) == []
    bad = {**good, "headings": good["headings"][::-1], "severities": [("High", "FCEBDD")] * 2}
    problems = check_docx(expected, bad)
    assert any("headings" in p for p in problems)
    assert any("severity cell" in p for p in problems)


def _container_running() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(  # noqa: S603, S607
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return probe.stdout.strip() == "true"


@pytest.mark.skipif(not _container_running(), reason="PwnDoc backend container not running")
def test_real_generator_sorts_shuffled_audit(tmp_path: Path) -> None:
    from build_pwndoc_template import build_template

    template = tmp_path / "tpl.docx"
    build_template().save(str(template))
    audit = tmp_path / "audit.json"
    data = sample_shuffled()
    audit.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "out.docx"
    subprocess.run(  # noqa: S603, S607
        ["sh", str(RENDER_SH), str(template), str(out), str(audit), "--filters", str(FILTERS)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    assert check_docx(expected_order(data), docx_view(out)) == []
