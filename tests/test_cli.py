import json
from types import SimpleNamespace

import httpx
import pytest

from pwndoc_ai import cli, pipeline
from pwndoc_ai.llm import Usage
from pwndoc_ai.pwndoc_client import PwnDocClient

AUDIT_FINDINGS = [
    {"_id": "f1", "title": "XSS", "description": "draft"},
    {"_id": "f2", "title": "SQLi", "description": "draft"},
]


class Env:
    """Fake PwnDoc + refiner wired into the CLI factories."""

    def __init__(self):
        self.puts: list[tuple[str, dict]] = []
        self.logins: list[dict] = []
        self.options: list[SimpleNamespace] = []
        self.refined: list[str] = []
        self.settings_seen = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/users/token":
            self.logins.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success", "datas": {"ok": True}})
        if path == "/api/audits":
            return httpx.Response(
                200, json={"status": "success", "datas": [{"_id": "A1", "name": "Audit One"}]}
            )
        if path == "/api/audits/A1/generate":
            return httpx.Response(200, content=b"DOCX-BYTES")
        if path == "/api/audits/A1":
            return httpx.Response(
                200, json={"status": "success", "datas": {"findings": AUDIT_FINDINGS}}
            )
        if request.method == "PUT":
            self.puts.append((path, json.loads(request.content)))
            return httpx.Response(200, json={"status": "success", "datas": "ok"})
        return httpx.Response(404)


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = Env()
    monkeypatch.delenv("PWNDOC_PASSWORD", raising=False)
    monkeypatch.delenv("PWNDOC_USERNAME", raising=False)
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda settings: PwnDocClient(
            "https://pwndoc.test", transport=httpx.MockTransport(fake.handler)
        ),
    )

    def make_refiner(settings, data_dir):
        fake.settings_seen.append(settings)
        return SimpleNamespace(
            refine=lambda finding, options: (
                fake.refined.append(finding["title"])
                or SimpleNamespace(payload={"description": "AI"}, usage=Usage(1, 1))
            )
        )

    monkeypatch.setattr(cli, "make_refiner", make_refiner)

    def make_options(fields, mode):
        options = SimpleNamespace(fields=fields, mode=mode)
        fake.options.append(options)
        return options

    monkeypatch.setattr(cli, "make_options", make_options)
    monkeypatch.setattr(pipeline, "_merge", lambda f, p: {**f, **p})
    monkeypatch.setattr(
        pipeline, "_update_payload", lambda f: {k: v for k, v in f.items() if k != "_id"}
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "s3cret")
    return fake


def script_inputs(monkeypatch, answers):
    queue = list(answers)

    def fake_input(prompt=""):
        assert queue, f"unexpected prompt: {prompt!r}"
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return queue


def run(tmp_path, *argv):
    return cli.main(["--data-dir", str(tmp_path / "data"), *argv])


def test_wizard_audit_source_with_upload(env, monkeypatch, tmp_path, capsys):
    queue = script_inputs(
        monkeypatch,
        [
            "1",  # source: audit
            "tester",  # username
            "",  # totp
            "1",  # audit number
            str(tmp_path / "out.json"),  # output
            "",  # fields -> all
            "",  # mode -> improve
            "n",  # web
            "n",  # rag
            "y",  # upload
            "y",  # confirm upload
        ],
    )
    assert run(tmp_path, "refine") == 0
    assert queue == []
    assert env.logins == [{"username": "tester", "password": "s3cret", "totpToken": ""}]
    assert env.refined == ["XSS", "SQLi"]
    assert env.options[0].mode == "improve" and "title" in env.options[0].fields
    assert [p for p, _ in env.puts] == ["/api/audits/A1/findings/f1", "/api/audits/A1/findings/f2"]
    assert env.puts[0][1]["description"] == "AI"
    assert env.settings_seen[0].use_web_search is False
    assert (tmp_path / "data/exports/audit_A1_findings.json").exists()
    assert "Audit One" in capsys.readouterr().out


def test_wizard_file_source_without_upload(env, monkeypatch, tmp_path):
    src = tmp_path / "findings.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    queue = script_inputs(
        monkeypatch,
        [
            "2",  # source: file
            "/no/such/file.json",  # invalid path is rejected, asked again
            str(src),
            "",  # output default <stem>_refined.json
            "title, poc",
            "2",  # mode fill_missing
            "n",
            "n",
            "n",  # upload
        ],
    )
    assert run(tmp_path, "refine") == 0
    assert queue == []
    assert env.logins == []  # never logged in: no PwnDoc needed
    assert env.options[0].fields == ("title", "poc") and env.options[0].mode == "fill_missing"
    out = tmp_path / "findings_refined.json"
    assert [f["description"] for f in json.loads(out.read_text())] == ["AI", "AI"]
    assert env.puts == []


def test_fully_flagged_run_never_prompts(env, monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"prompted: {prompt}"))
    src = tmp_path / "findings.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    code = run(
        tmp_path,
        "refine",
        "--input",
        str(src),
        "--output",
        str(tmp_path / "o.json"),
        "--fields",
        "all",
        "--mode",
        "improve",
        "--no-web",
        "--no-rag",
        "--no-upload",
    )
    assert code == 0 and env.refined == ["XSS", "SQLi"]


def test_flagged_upload_uses_env_password_and_yes(env, monkeypatch, tmp_path):
    monkeypatch.setenv("PWNDOC_PASSWORD", "from-env")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("should not prompt"))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"prompted: {prompt}"))
    code = run(
        tmp_path,
        "refine",
        "--audit",
        "A1",
        "--username",
        "u",
        "--totp",
        "",
        "--output",
        str(tmp_path / "o.json"),
        "--fields",
        "poc",
        "--mode",
        "improve",
        "--no-web",
        "--no-rag",
        "--upload",
        "--yes",
    )
    assert code == 0
    assert env.logins[0]["password"] == "from-env"
    assert len(env.puts) == 2


def test_existing_output_can_be_resumed_or_replaced(env, monkeypatch, tmp_path):
    out = tmp_path / "o.json"
    out.write_text(json.dumps([{**AUDIT_FINDINGS[0], "description": "kept"}]))
    src = tmp_path / "f.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    base = [
        "refine",
        "--input",
        str(src),
        "--output",
        str(out),
        "--fields",
        "all",
        "--mode",
        "improve",
        "--no-web",
        "--no-rag",
        "--no-upload",
    ]

    script_inputs(monkeypatch, ["y"])  # resume
    assert run(tmp_path, *base) == 0
    assert env.refined == ["SQLi"]

    script_inputs(monkeypatch, ["n"])  # replace -> old file backed up, everything redone
    assert run(tmp_path, *base) == 0
    assert len(list(tmp_path.glob("o.json.*.bak"))) == 1
    assert env.refined == ["SQLi", "XSS", "SQLi"]


def test_partial_failure_returns_exit_3(env, monkeypatch, tmp_path):
    def failing_refiner(settings, data_dir):
        def refine(finding, options):
            if finding["title"] == "SQLi":
                raise RuntimeError("bad")
            return SimpleNamespace(payload={}, usage=Usage(1, 1))

        return SimpleNamespace(refine=refine)

    monkeypatch.setattr(cli, "make_refiner", failing_refiner)
    src = tmp_path / "f.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    code = run(
        tmp_path,
        "refine",
        "--input",
        str(src),
        "--output",
        str(tmp_path / "o.json"),
        "--fields",
        "all",
        "--mode",
        "improve",
        "--no-web",
        "--no-rag",
        "--no-upload",
    )
    assert code == cli.EXIT_PARTIAL


def test_unknown_field_is_a_friendly_error(env, tmp_path, capsys):
    src = tmp_path / "f.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    code = run(
        tmp_path,
        "refine",
        "--input",
        str(src),
        "--output",
        str(tmp_path / "o.json"),
        "--fields",
        "nonsense",
    )
    assert code == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error: unknown field(s): nonsense")


def test_upload_command_skips_unknown_ids(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PWNDOC_PASSWORD", "pw")
    path = tmp_path / "r.json"
    path.write_text(json.dumps([*AUDIT_FINDINGS, {"_id": "zzz", "title": "Ghost"}]))
    code = run(
        tmp_path, "upload", "--file", str(path), "--audit", "A1", "--username", "u", "--totp", ""
    )
    assert code == cli.EXIT_PARTIAL
    assert len(env.puts) == 2
    assert "Ghost" in capsys.readouterr().out


def test_export_writes_docx(env, tmp_path, monkeypatch):
    monkeypatch.setenv("PWNDOC_PASSWORD", "pw")
    out = tmp_path / "report.docx"
    code = run(
        tmp_path, "export", "--audit", "A1", "--out", str(out), "--username", "u", "--totp", ""
    )
    assert code == 0 and out.read_bytes() == b"DOCX-BYTES"


def test_config_set_and_masked_show(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "sk-topsecret")
    assert run(tmp_path, "config", "--set", "llm_model=my-model", "--set", "llm_api_key=-") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["llm_model"] == "my-model"
    assert shown["llm_api_key"] == "********" and "sk-topsecret" not in json.dumps(shown)
    stored = json.loads((tmp_path / "data/config.json").read_text())
    assert stored["llm_api_key"] == "sk-topsecret"


def test_config_rejects_bad_value(tmp_path, capsys):
    assert run(tmp_path, "config", "--set", "temperature=99") == cli.EXIT_ERROR
    assert "temperature" in capsys.readouterr().err


def test_config_requires_key_value(tmp_path, capsys):
    assert run(tmp_path, "config", "--set", "oops") == cli.EXIT_ERROR


def test_serve_dispatch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_run_server", lambda *a: calls.append(a))
    assert run(tmp_path, "serve", "--port", "9000", "--https") == 0
    assert calls == [("0.0.0.0", 9000, str(tmp_path / "data"), True, False)]


def test_serve_insecure_http_flag_is_forwarded(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_run_server", lambda *a: calls.append(a))
    assert run(tmp_path, "serve", "--insecure-http") == 0
    assert calls[0][3:] == (False, True)


def test_serve_refusal_is_a_friendly_one_line_error(monkeypatch, tmp_path, capsys):
    from pwndoc_ai import server

    def refuse(**kwargs):
        raise server.InsecureBindError("refusing to serve plain HTTP on 0.0.0.0")

    monkeypatch.setattr(server, "run", refuse)
    assert run(tmp_path, "serve") == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error: refusing to serve plain HTTP") and "Traceback" not in err


@pytest.mark.parametrize("exc_name", ["TlsUnavailable", "RagUnavailable"])
def test_missing_optional_extras_exit_cleanly(monkeypatch, tmp_path, capsys, exc_name):
    from pwndoc_ai import rag, tls

    exc = {"TlsUnavailable": tls.TlsUnavailable, "RagUnavailable": rag.RagUnavailable}[exc_name]

    def boom(*args):
        raise exc("install the extra")

    monkeypatch.setattr(cli, "_run_server", boom)
    assert run(tmp_path, "serve", "--https") == cli.EXIT_ERROR
    assert "error: install the extra" in capsys.readouterr().err


def test_yes_without_upload_flag_is_refine_only_and_never_prompts(env, monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"prompted: {prompt}"))
    code = run(
        tmp_path,
        "refine",
        "--audit",
        "A1",
        "--username",
        "u",
        "--totp",
        "",
        "--output",
        str(tmp_path / "o.json"),
        "--fields",
        "poc",
        "--mode",
        "improve",
        "--no-web",
        "--no-rag",
        "--yes",
    )
    assert code == 0 and env.puts == [] and env.refined == ["XSS", "SQLi"]


def test_replacing_output_never_overwrites_an_earlier_backup(env, monkeypatch, tmp_path):
    out = tmp_path / "o.json"
    src = tmp_path / "f.json"
    src.write_text(json.dumps(AUDIT_FINDINGS))
    base = [
        "refine", "--input", str(src), "--output", str(out), "--fields", "all",
        "--mode", "improve", "--no-web", "--no-rag", "--no-upload",
    ]  # fmt: skip
    for round_ in range(2):
        out.write_text(json.dumps([{**AUDIT_FINDINGS[0], "description": f"gen{round_}"}]))
        script_inputs(monkeypatch, ["n"])  # do not resume: move the old file aside
        assert run(tmp_path, *base) == 0
    backups = sorted(tmp_path.glob("o.json.*.bak"))
    assert len(backups) == 2
    contents = {json.loads(b.read_text())[0]["description"] for b in backups}
    assert contents == {"gen0", "gen1"}


def test_api_key_on_command_line_warns_but_prompt_form_does_not(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "sk-from-prompt")
    assert run(tmp_path, "config", "--set", "llm_api_key=sk-in-argv") == 0
    assert "shell history" in capsys.readouterr().err
    assert run(tmp_path, "config", "--set", "llm_api_key=-") == 0
    assert "shell history" not in capsys.readouterr().err


def test_eof_and_interrupt_codes(env, monkeypatch, tmp_path):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run(tmp_path, "refine") == cli.EXIT_USAGE

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert run(tmp_path, "refine") == cli.EXIT_INTERRUPTED


def test_missing_input_file_flag_is_error(env, tmp_path, capsys):
    assert run(tmp_path, "refine", "--input", str(tmp_path / "nope.json")) == cli.EXIT_ERROR
    assert "not found" in capsys.readouterr().err
