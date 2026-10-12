import json
from types import SimpleNamespace

import httpx
import pytest

from pwndoc_ai import pipeline
from pwndoc_ai.jsonio import read_findings
from pwndoc_ai.llm import LlmError, Usage
from pwndoc_ai.pipeline import ProgressEvent, refine_file, upload_file
from pwndoc_ai.pwndoc_client import PwnDocClient

FINDINGS = [
    {"_id": "f1", "title": "XSS", "description": "draft"},
    {"_id": "f2", "title": "SQLi", "description": "draft"},
    {"_id": "f3", "title": "IDOR", "description": "draft"},
]


@pytest.fixture(autouse=True)
def stub_refiner_helpers(monkeypatch):
    monkeypatch.setattr(pipeline, "_merge", lambda finding, payload: {**finding, **payload})
    monkeypatch.setattr(
        pipeline, "_update_payload", lambda f: {k: v for k, v in f.items() if k != "_id"}
    )


class FakeRefiner:
    def __init__(self, fail_titles=(), seen_output=None):
        self.fail_titles = set(fail_titles)
        self.calls: list[str] = []
        self.seen_output = seen_output  # callable capturing the output file between calls

    def refine(self, finding, options):
        self.calls.append(finding["title"])
        if self.seen_output:
            self.seen_output()
        if finding["title"] in self.fail_titles:
            raise LlmError("boom")
        return SimpleNamespace(
            payload={"description": f"refined {finding['title']}"}, usage=Usage(10, 5)
        )


def write_input(tmp_path, findings=FINDINGS):
    path = tmp_path / "in.json"
    path.write_text(json.dumps(findings))
    return path


def test_refines_all_and_sums_usage(tmp_path):
    out = tmp_path / "out.json"
    summary = refine_file(write_input(tmp_path), out, FakeRefiner(), None)
    assert (summary.total, summary.refined, summary.skipped, summary.failed) == (3, 3, 0, ())
    assert summary.usage == Usage(30, 15)
    assert [f["description"] for f in read_findings(out)] == [
        "refined XSS",
        "refined SQLi",
        "refined IDOR",
    ]
    assert read_findings(out)[0]["_id"] == "f1"


def test_resume_skips_findings_already_in_output(tmp_path):
    out = tmp_path / "out.json"
    out.write_text(json.dumps([{"_id": "f1", "title": "XSS", "description": "kept"}]))
    refiner = FakeRefiner()
    summary = refine_file(write_input(tmp_path), out, refiner, None)
    assert refiner.calls == ["SQLi", "IDOR"]
    assert (summary.skipped, summary.refined) == (1, 2)
    by_id = {f["_id"]: f for f in read_findings(out)}
    assert by_id["f1"]["description"] == "kept"  # untouched, not re-sent to the LLM


def test_failure_is_isolated_and_reported(tmp_path):
    out = tmp_path / "out.json"
    summary = refine_file(write_input(tmp_path), out, FakeRefiner(fail_titles={"SQLi"}), None)
    assert summary.refined == 2
    assert summary.failed == (("f2", "boom"),)
    assert [f["_id"] for f in read_findings(out)] == ["f1", "f3"]


def test_failed_finding_is_retried_on_rerun(tmp_path):
    out = tmp_path / "out.json"
    inp = write_input(tmp_path)
    refine_file(inp, out, FakeRefiner(fail_titles={"SQLi"}), None)
    refiner = FakeRefiner()
    summary = refine_file(inp, out, refiner, None)
    assert refiner.calls == ["SQLi"]
    assert summary.skipped == 2 and summary.refined == 1


def test_output_written_after_each_finding(tmp_path):
    out = tmp_path / "out.json"
    sizes: list[int] = []
    refiner = FakeRefiner(
        seen_output=lambda: sizes.append(len(read_findings(out)) if out.exists() else 0)
    )
    refine_file(write_input(tmp_path), out, refiner, None)
    assert sizes == [0, 1, 2]
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_events(tmp_path):
    events: list[ProgressEvent] = []
    out = tmp_path / "out.json"
    out.write_text(json.dumps([FINDINGS[0]]))
    refine_file(write_input(tmp_path), out, FakeRefiner(fail_titles={"IDOR"}), None, events.append)
    assert [(e.index, e.status) for e in events] == [(1, "skipped"), (2, "refined"), (3, "failed")]
    assert events[2].message == "boom"


def test_usage_file_written_next_to_output(tmp_path):
    out = tmp_path / "sub" / "out.json"
    refine_file(write_input(tmp_path), out, FakeRefiner(), None)
    (usage_file,) = list(out.parent.glob("tokens_used_*.json"))
    data = json.loads(usage_file.read_text())
    assert data["total_tokens"] == 45
    assert len(data["per_finding"]) == 3


def test_usage_file_can_be_disabled(tmp_path):
    out = tmp_path / "out.json"
    refine_file(write_input(tmp_path), out, FakeRefiner(), None, write_usage_file=False)
    assert not list(tmp_path.glob("tokens_used_*.json"))


# -- upload -------------------------------------------------------------------------------------
def make_client(existing_ids, put_status=200):
    puts: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/audits/A1":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "datas": {"findings": [{"_id": i} for i in existing_ids]},
                },
            )
        if request.method == "PUT":
            puts.append((request.url.path, json.loads(request.content)))
            if put_status != 200:
                return httpx.Response(
                    put_status, json={"status": "error", "datas": {"msg": "nope"}}
                )
            return httpx.Response(200, json={"status": "success", "datas": "ok"})
        return httpx.Response(404)

    client = PwnDocClient("https://pwndoc.test", transport=httpx.MockTransport(handler))
    return client, puts


def test_upload_skips_findings_not_in_audit(tmp_path):
    client, puts = make_client(["f1", "f3"])
    path = tmp_path / "refined.json"
    path.write_text(json.dumps(FINDINGS))
    ok, failed = upload_file(path, client, "A1")
    assert ok == ("f1", "f3")
    assert failed == (("f2", "not part of the target audit"),)
    assert [p for p, _ in puts] == [
        "/api/audits/A1/findings/f1",
        "/api/audits/A1/findings/f3",
    ]
    assert "_id" not in puts[0][1] and puts[0][1]["title"] == "XSS"


def test_upload_reports_api_errors_and_continues(tmp_path):
    client, puts = make_client(["f1"], put_status=403)
    path = tmp_path / "refined.json"
    path.write_text(json.dumps(FINDINGS[:1]))
    events: list[ProgressEvent] = []
    ok, failed = upload_file(path, client, "A1", events.append)
    assert ok == ()
    assert failed[0][0] == "f1" and "403" in failed[0][1]
    assert events[0].status == "failed"
    assert len(puts) == 1


def test_upload_finding_without_id_is_reported(tmp_path):
    client, puts = make_client(["f1"])
    path = tmp_path / "refined.json"
    path.write_text(json.dumps([{"title": "No id"}]))
    ok, failed = upload_file(path, client, "A1")
    assert ok == () and failed == (("No id", "not part of the target audit"),)
    assert puts == []


def test_duplicate_titles_without_ids_are_each_refined_and_resumable(tmp_path):
    dup = [{"title": "XSS", "description": "a"}, {"title": "XSS", "description": "b"}]
    src, out = write_input(tmp_path, dup), tmp_path / "out.json"
    first = refine_file(src, out, FakeRefiner(), None, write_usage_file=False)
    assert (first.refined, first.skipped) == (2, 0)
    assert len(read_findings(out)) == 2
    again = refine_file(src, out, FakeRefiner(), None, write_usage_file=False)
    assert (again.refined, again.skipped) == (0, 2)
