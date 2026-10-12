import dataclasses
import json
import sys
import types
from pathlib import Path

import pytest

from pwndoc_ai.jobs import JobConflict, JobManager, JobNotFound
from pwndoc_ai.llm import LlmError, Usage


@pytest.fixture(scope="module", autouse=True)
def refiner_api():
    """Use the real refiner module if present, else a minimal stand-in with the same contract."""
    try:
        import pwndoc_ai.refiner as module

        yield module
        return
    except ImportError:
        pass
    stub = types.ModuleType("pwndoc_ai.refiner")

    @dataclasses.dataclass(frozen=True)
    class RefineOptions:
        fields: tuple = ("description",)
        mode: str = "improve"

    @dataclasses.dataclass(frozen=True)
    class Proposal:
        finding_id: str
        title: str
        payload: dict
        changes: dict
        usage: Usage

    stub.RefineOptions, stub.Proposal = RefineOptions, Proposal
    stub.finding_update_payload = lambda f: {k: v for k, v in f.items() if k != "_id"}
    sys.modules["pwndoc_ai.refiner"] = stub
    yield stub
    del sys.modules["pwndoc_ai.refiner"]


FINDINGS = [
    {"_id": "f1", "title": "XSS", "description": "old1"},
    {"_id": "f2", "title": "boom", "description": "old2"},
    {"_id": "f3", "title": "SQLi", "description": "old3"},
]


class FakeClient:
    def __init__(self, log):
        self.log = log

    def list_findings(self, audit_id):
        return FINDINGS

    def update_finding(self, audit_id, finding_id, payload):
        if finding_id == "f3":
            raise RuntimeError("PwnDoc 403: nope")
        self.log.append((audit_id, finding_id, payload))

    def close(self):
        self.log.append("closed")


class FakeRefiner:
    def __init__(self, api):
        self.api = api

    def refine(self, finding, options):
        if finding["title"] == "boom":
            raise LlmError("model exploded")
        return self.api.Proposal(
            finding_id=finding["_id"],
            title=finding["title"],
            payload={"description": f"<p>new {finding['_id']}</p>"},
            changes={"description": {"before": finding["description"], "after": "new"}},
            usage=Usage(10, 5),
        )


@pytest.fixture
def env(tmp_path, refiner_api):
    log: list = []
    manager = JobManager(
        tmp_path, lambda: FakeRefiner(refiner_api), lambda cookies: FakeClient(log)
    )
    return manager, log, tmp_path, refiner_api


def run(manager, api, owner="alice", ids=None, auto=False):
    job = manager.start(owner, {"token": "t"}, "audit1", ids, api.RefineOptions(), auto)
    job.thread.join(5)
    return job


def test_job_proposes_without_writing(env):
    manager, log, _, api = env
    job = run(manager, api)
    view = job.view()
    assert view["status"] == "done"
    assert [i["status"] for i in view["items"]] == ["proposed", "failed", "proposed"]
    assert view["items"][1]["error"] == "model exploded"
    assert view["usage"] == {"input": 20, "output": 10}
    assert view["items"][0]["changes"]["description"]["after"] == "new"
    assert [e for e in log if e != "closed"] == []


def test_finding_ids_filter(env):
    manager, _, _, api = env
    view = run(manager, api, ids=["f1"]).view()
    assert [i["finding_id"] for i in view["items"]] == ["f1"]


def test_no_matching_findings_is_error(env):
    manager, _, _, api = env
    view = run(manager, api, ids=["zzz"]).view()
    assert view["status"] == "error"
    assert "no matching" in view["error"]


def test_apply_writes_backup_then_puts(env):
    manager, log, tmp, api = env
    job = run(manager, api)
    manager.apply(job, {"token": "t"}, ["f1", "f3", "f2"])
    items = {i["finding_id"]: i for i in job.view()["items"]}
    assert items["f1"]["status"] == "applied"
    assert items["f3"]["status"] == "failed" and "403" in items["f3"]["error"]
    assert items["f2"]["status"] == "failed"  # never had a proposal, stays failed
    puts = [e for e in log if e != "closed"]
    assert puts == [("audit1", "f1", {"description": "<p>new f1</p>"})]
    backups = list((tmp / "backups" / "audit1").glob("*.json"))
    assert len(backups) == 1
    assert {f["_id"] for f in json.loads(backups[0].read_text())} == {"f1", "f2", "f3"}


def test_backup_written_once_per_job(env):
    manager, _, tmp, api = env
    job = run(manager, api)
    manager.apply(job, {"token": "t"}, ["f1"])
    manager.apply(job, {"token": "t"}, ["f3"])
    assert len(list(Path(tmp / "backups" / "audit1").glob("*.json"))) == 1


def test_auto_apply_puts_each_finding(env):
    manager, log, _, api = env
    job = run(manager, api, auto=True)
    statuses = [i["status"] for i in job.view()["items"]]
    assert statuses == ["applied", "failed", "failed"]
    assert [e[1] for e in log if e != "closed"] == ["f1"]


def test_apply_rejects_unknown_finding(env):
    manager, _, _, api = env
    job = run(manager, api)
    with pytest.raises(ValueError):
        manager.apply(job, {"token": "t"}, ["nope"])


def test_other_user_cannot_see_job(env):
    manager, _, _, api = env
    job = run(manager, api)
    assert manager.get(job.id, "alice") is job
    with pytest.raises(JobNotFound):
        manager.get(job.id, "mallory")


def test_one_running_job_per_user(tmp_path, refiner_api):
    import threading

    gate = threading.Event()

    class Slow(FakeRefiner):
        def refine(self, finding, options):
            gate.wait(5)
            return super().refine(finding, options)

    manager = JobManager(tmp_path, lambda: Slow(refiner_api), lambda c: FakeClient([]))
    first = manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())
    with pytest.raises(JobConflict):
        manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())
    manager.start("bob", {}, "audit1", None, refiner_api.RefineOptions())  # other users unaffected
    manager.cancel(first)
    gate.set()
    first.thread.join(5)
    assert first.status == "cancelled"


def test_invalid_ids_rejected(env):
    manager, _, _, api = env
    with pytest.raises(ValueError):
        manager.start("alice", {}, "../etc", None, api.RefineOptions())
    with pytest.raises(ValueError):
        manager.start("alice", {}, "audit1", ["a/b"], api.RefineOptions())


def test_old_jobs_pruned(tmp_path, refiner_api):
    manager = JobManager(
        tmp_path, lambda: FakeRefiner(refiner_api), lambda c: FakeClient([]), max_jobs=2
    )
    jobs = []
    for _ in range(4):
        job = manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())
        job.thread.join(5)
        jobs.append(job)
    with pytest.raises(JobNotFound):
        manager.get(jobs[0].id, "alice")
    assert manager.get(jobs[-1].id, "alice") is jobs[-1]


def test_client_factory_failure_marks_error(tmp_path, refiner_api):
    def broken(_):
        raise RuntimeError("cannot reach PwnDoc")

    manager = JobManager(tmp_path, lambda: FakeRefiner(refiner_api), broken)
    job = manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())
    job.thread.join(5)
    assert job.view()["status"] == "error"
    assert "cannot reach" in job.view()["error"]


# -- conflict-safe apply, live backup, session handling ------------------------------------------
import copy  # noqa: E402
import threading  # noqa: E402

from pwndoc_ai.jobs import CHANGED_SINCE, SESSION_EXPIRED  # noqa: E402


def cf(label, text="", cid="c"):
    return {"customField": {"_id": cid, "label": label, "fieldType": "input"}, "text": text}


def base_finding(fid="f1", **over):
    return {
        "_id": fid,
        "title": "XSS",
        "description": "old",
        "observation": "",
        "customFields": [cf("CWE", "", "c1"), cf("Severity", "", "c2")],
        **over,
    }


class LiveClient:
    """list_findings reads mutable state, so tests can edit PwnDoc between propose and apply."""

    def __init__(self, state, puts, put_status=None):
        self.state, self.puts, self.put_status = state, puts, put_status
        self.closed = False

    def list_findings(self, audit_id):
        return copy.deepcopy(self.state["findings"])

    def update_finding(self, audit_id, finding_id, payload):
        if self.put_status:
            err = RuntimeError("PwnDoc 401: Expired token")
            err.status = self.put_status
            raise err
        self.puts.append((finding_id, payload))

    def close(self):
        self.closed = True


class CfRefiner:
    def __init__(self, api):
        self.api, self.closed = api, False

    def refine(self, finding, options):
        entries = [
            {**e, "text": "CWE-79"} if e["customField"]["label"] == "CWE" else e
            for e in finding["customFields"]
        ]
        return self.api.Proposal(
            finding["_id"],
            finding["title"],
            {"description": "<p>new</p>", "customFields": entries},
            {
                "description": {"before": "old", "after": "new"},
                "customFields: CWE": {"before": "", "after": "CWE-79"},
            },
            Usage(1, 1),
        )

    def close(self):
        self.closed = True


def live_env(tmp_path, api, findings, put_status=None, refiner=None):
    state, puts = {"findings": findings}, []
    holder = {"refiner": refiner or CfRefiner(api)}
    client = LiveClient(state, puts, put_status)
    manager = JobManager(tmp_path, lambda: holder["refiner"], lambda cookies: client)
    return manager, state, puts, holder["refiner"], client


def run_job(manager, api, **kw):
    job = manager.start("alice", {"token": "t"}, "audit1", None, api.RefineOptions(), **kw)
    job.thread.join(5)
    return job


def test_apply_merges_custom_fields_onto_live_and_keeps_concurrent_edits(tmp_path, refiner_api):
    manager, state, puts, _, _ = live_env(tmp_path, refiner_api, [base_finding()])
    job = run_job(manager, refiner_api)
    # someone edits Severity in PwnDoc while the user reviews the preview
    state["findings"][0]["customFields"][1]["text"] = "High"
    manager.apply(job, {"token": "t"}, ["f1"])
    assert job.view()["items"][0]["status"] == "applied"
    ((fid, payload),) = puts
    texts = {e["customField"]["label"]: e["text"] for e in payload["customFields"]}
    assert texts == {"CWE": "CWE-79", "Severity": "High"}
    assert payload["description"] == "<p>new</p>"


def test_apply_refuses_to_overwrite_a_field_edited_since_the_proposal(tmp_path, refiner_api):
    manager, state, puts, _, _ = live_env(tmp_path, refiner_api, [base_finding()])
    job = run_job(manager, refiner_api)
    state["findings"][0]["description"] = "edited by a colleague"
    manager.apply(job, {"token": "t"}, ["f1"])
    item = job.view()["items"][0]
    assert item["status"] == "failed" and item["error"] == CHANGED_SINCE
    assert puts == []


def test_backup_holds_live_state_and_has_job_id_in_name(tmp_path, refiner_api):
    manager, state, _, _, _ = live_env(tmp_path, refiner_api, [base_finding()])
    job = run_job(manager, refiner_api)
    state["findings"][0]["observation"] = "added later"  # unrelated field: no conflict
    manager.apply(job, {"token": "t"}, ["f1"])
    (backup,) = (tmp_path / "backups" / "audit1").glob("*.json")
    assert job.id[:8] in backup.name and len(backup.stem.split("_")[0]) == len(
        "20260101T000000000000Z"
    )
    assert json.loads(backup.read_text())[0]["observation"] == "added later"


def test_manual_apply_reports_skipped_ids(tmp_path, refiner_api):
    manager, _, _, _, _ = live_env(tmp_path, refiner_api, [base_finding()])
    job = run_job(manager, refiner_api)
    manager.apply(job, {"token": "t"}, ["f1"])
    manager.apply(job, {"token": "t"}, ["f1"])  # already applied
    assert job.view()["skipped"] == ["f1"]


def test_apply_on_expired_session_leaves_proposals_applicable(tmp_path, refiner_api):
    findings = [base_finding("f1"), base_finding("f2")]
    manager, _, puts, _, _ = live_env(tmp_path, refiner_api, findings, put_status=401)
    job = run_job(manager, refiner_api)
    manager.apply(job, {"token": "t"}, ["f1", "f2"])
    items = job.view()["items"]
    assert [i["status"] for i in items] == ["proposed", "proposed"]
    assert all(i["error"] == SESSION_EXPIRED for i in items)
    assert puts == []


def test_auto_apply_stops_after_401_but_keeps_proposals(tmp_path, refiner_api):
    findings = [base_finding("f1"), base_finding("f2"), base_finding("f3")]
    manager, _, _, _, client = live_env(tmp_path, refiner_api, findings, put_status=401)
    job = run_job(manager, refiner_api, auto_apply=True)
    items = job.view()["items"]
    assert [i["status"] for i in items] == ["proposed"] * 3
    assert all(i["error"] == SESSION_EXPIRED for i in items)
    assert job.view()["status"] == "done"


def test_refiner_and_client_are_closed_when_job_ends(tmp_path, refiner_api):
    manager, _, _, refiner, client = live_env(tmp_path, refiner_api, [base_finding()])
    run_job(manager, refiner_api)
    assert refiner.closed and client.closed


def test_cancelled_job_does_not_block_the_next_one(tmp_path, refiner_api):
    gate = threading.Event()

    class Slow(CfRefiner):
        def refine(self, finding, options):
            gate.wait(5)
            return super().refine(finding, options)

    manager, *_ = live_env(tmp_path, refiner_api, [base_finding()], refiner=Slow(refiner_api))
    first = manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())
    manager.cancel(first)
    second = manager.start("alice", {}, "audit1", None, refiner_api.RefineOptions())  # no 409
    gate.set()
    first.thread.join(5)
    second.thread.join(5)


def test_global_running_job_cap(tmp_path, refiner_api):
    gate = threading.Event()

    class Slow(CfRefiner):
        def refine(self, finding, options):
            gate.wait(5)
            return super().refine(finding, options)

    state = {"findings": [base_finding()]}
    manager = JobManager(
        tmp_path,
        lambda: Slow(refiner_api),
        lambda c: LiveClient(state, []),
        max_running=2,
    )
    opts = refiner_api.RefineOptions()
    jobs = [manager.start(user, {}, "audit1", None, opts) for user in ("a", "b")]
    with pytest.raises(JobConflict, match="busy"):
        manager.start("c", {}, "audit1", None, opts)
    gate.set()
    for job in jobs:
        job.thread.join(5)
    manager.start("c", {}, "audit1", None, opts).thread.join(5)  # capacity is free again


def test_cookie_callable_is_resolved_for_each_auto_apply(tmp_path, refiner_api):
    seen = []
    state, puts = {"findings": [base_finding("f1"), base_finding("f2")]}, []

    def client_factory(cookies):
        seen.append(dict(cookies))
        return LiveClient(state, puts)

    manager = JobManager(tmp_path, lambda: CfRefiner(refiner_api), client_factory)
    counter = iter(range(100))
    job = manager.start(
        "alice",
        lambda: {"token": f"jwt{next(counter)}"},
        "audit1",
        None,
        refiner_api.RefineOptions(),
        True,
    )
    job.thread.join(5)
    assert [i["status"] for i in job.view()["items"]] == ["applied", "applied"]
    assert len({c["token"] for c in seen}) == len(seen) >= 3  # list + one per auto-apply
