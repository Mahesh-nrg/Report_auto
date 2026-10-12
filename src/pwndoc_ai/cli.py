"""Command line interface.

Subcommands: ``serve``, ``refine`` (interactive wizard), ``upload``, ``config``, ``export``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import ConfigStore, Settings, SettingsError, apply_patch, effective_verify_tls
from .jsonio import JsonFileError, read_findings, write_json_atomic
from .llm import LlmClient, LlmError
from .pipeline import (
    STATUS_FAILED,
    STATUS_REFINED,
    STATUS_SKIPPED,
    STATUS_UPLOADED,
    PipelineSummary,
    ProgressEvent,
    refine_file,
    upload_file,
)
from .prompts import ALL_FIELDS
from .pwndoc_client import PwnDocClient, PwnDocError
from .rag import RagUnavailable
from .tls import TlsUnavailable

log = logging.getLogger("pwndoc_ai")

MODES = (
    ("improve", "Rewrite every selected field"),
    ("fill_missing", "Only fill fields that are empty"),
)
SOURCES = (
    ("audit", "Pull findings from a PwnDoc audit"),
    ("file", "Use an existing findings JSON file"),
)
EXIT_ERROR, EXIT_USAGE, EXIT_PARTIAL, EXIT_INTERRUPTED = 1, 2, 3, 130


class CliError(RuntimeError):
    """A user-facing problem reported as a single ``error:`` line."""


# -- factories (patched in tests) ---------------------------------------------------------------
def make_client(settings: Settings) -> PwnDocClient:
    return PwnDocClient(settings.pwndoc_url, effective_verify_tls(settings))


def make_refiner(settings: Settings, data_dir: Path) -> Any:
    from .context import make_context_provider
    from .rag import DocumentIndex
    from .refiner import FindingRefiner

    rag = DocumentIndex(data_dir / "rag") if settings.use_rag else None
    return FindingRefiner(LlmClient(settings), settings, make_context_provider(settings, rag))


def make_options(fields: tuple[str, ...], mode: str) -> Any:
    from .refiner import RefineOptions

    return RefineOptions(fields=fields, mode=mode)


def _run_server(
    host: str, port: int, data_dir: str | None, https: bool, insecure_http: bool = False
) -> None:
    from .server import InsecureBindError, run

    try:
        run(host=host, port=port, data_dir=data_dir, https=https, insecure_http=insecure_http)
    except InsecureBindError as exc:
        raise CliError(str(exc)) from exc


# -- prompt helpers -----------------------------------------------------------------------------
def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_choice(prompt: str, options: Sequence[tuple[str, str]], default: str | None = None) -> str:
    keys = [key for key, _ in options]
    for number, (_key, label) in enumerate(options, 1):
        print(f"  {number}) {label}")
    default = default or keys[0]
    while True:
        answer = ask(prompt, str(keys.index(default) + 1))
        if answer in keys:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        print(f"  Enter a number 1-{len(keys)}.")


def _yes_no_flag(flag: bool | None, prompt: str, default: bool) -> bool:
    return flag if flag is not None else ask_yes_no(prompt, default)


# -- PwnDoc session -----------------------------------------------------------------------------
class ClientSession:
    """Logs in to PwnDoc at most once and shares the client across wizard steps."""

    def __init__(self, settings: Settings, username: str | None, totp: str | None):
        self._settings, self._username, self._totp = settings, username, totp
        self._client: PwnDocClient | None = None

    def get(self) -> PwnDocClient:
        if self._client is None:
            username = (
                self._username
                or os.environ.get("PWNDOC_USERNAME")
                or ask("PwnDoc username", "admin")
            )
            password = os.environ.get("PWNDOC_PASSWORD") or getpass.getpass("PwnDoc password: ")
            totp = self._totp if self._totp is not None else ask("TOTP code (blank if not enabled)")
            client = make_client(self._settings)
            client.login(username, password, totp)
            self._client = client
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def choose_audit(client: PwnDocClient, preset: str | None, prompt: str) -> str:
    if preset:
        return preset
    audits = client.list_audits()
    if not audits:
        raise CliError("no audits found in PwnDoc")
    print(f"\n  {'#':>3}  {'Name':<40} ID")
    for number, audit in enumerate(audits, 1):
        print(f"  {number:>3}  {str(audit.get('name', ''))[:40]:<40} {audit.get('_id', '')}")
    while True:
        answer = ask(prompt)
        if answer.isdigit() and 1 <= int(answer) <= len(audits):
            return str(audits[int(answer) - 1]["_id"])
        print(f"  Enter a number 1-{len(audits)}.")


# -- refine wizard steps ------------------------------------------------------------------------
def _validated_findings_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    count = len(read_findings(path))
    if count == 0:
        raise CliError(f"{path} contains no findings")
    print(f"  Found {count} finding(s) in {path}")
    return path


def _prompt_input_path() -> Path:
    while True:
        try:
            return _validated_findings_path(ask("Path to findings JSON file"))
        except (JsonFileError, CliError, ValueError) as exc:
            print(f"  {exc}")


def resolve_source(
    args: argparse.Namespace, session: ClientSession, data_dir: Path
) -> tuple[Path, str | None]:
    """Return ``(findings_file, source_audit_id)``; audit findings are exported to ``data_dir``."""
    source = args.source or ("file" if args.input else "audit" if args.audit else None)
    source = source or ask_choice("Where do the findings come from?", SOURCES)
    if source == "file":
        path = _validated_findings_path(args.input) if args.input else _prompt_input_path()
        return path, None
    client = session.get()
    audit_id = choose_audit(client, args.audit, "Number of the audit to refine")
    findings = client.list_findings(audit_id)
    if not findings:
        raise CliError("that audit has no findings")
    path = data_dir / "exports" / f"audit_{audit_id}_findings.json"
    write_json_atomic(path, findings)
    print(f"  Pulled {len(findings)} finding(s) -> {path}")
    return path, audit_id


def _free_backup_path(out: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = out.with_name(f"{out.name}.{stamp}.bak")
    counter = 1
    while candidate.exists():
        candidate = out.with_name(f"{out.name}.{stamp}.{counter}.bak")
        counter += 1
    return candidate


def resolve_output(args: argparse.Namespace, in_path: Path) -> Path:
    default = in_path.with_name(f"{in_path.stem}_refined.json")
    out = (
        Path(args.output).expanduser()
        if args.output
        else Path(ask("Output JSON file", str(default)))
    )
    if out.exists() and not args.yes:
        count = len(read_findings(out)) if out.stat().st_size else 0
        if not ask_yes_no(f"{out} exists with {count} finding(s). Resume and keep them", True):
            backup = _free_backup_path(out)
            out.replace(backup)
            print(f"  Previous file moved to {backup}")
    return out


def parse_fields(text: str) -> tuple[str, ...]:
    if not text.strip() or text.strip().lower() == "all":
        return ALL_FIELDS
    names = tuple(dict.fromkeys(p.strip() for p in text.split(",") if p.strip()))
    unknown = [n for n in names if n not in ALL_FIELDS]
    if unknown:
        raise CliError(
            f"unknown field(s): {', '.join(unknown)} (choose from {', '.join(ALL_FIELDS)})"
        )
    return names


def resolve_fields(args: argparse.Namespace) -> tuple[str, ...]:
    if args.fields is not None:
        return parse_fields(args.fields)
    while True:
        try:
            return parse_fields(ask(f"Fields to update ({', '.join(ALL_FIELDS)}) or 'all'", "all"))
        except CliError as exc:
            print(f"  {exc}")


def resolve_settings(args: argparse.Namespace, base: Settings) -> Settings:
    web = _yes_no_flag(args.web, "Use internet search for context", base.use_web_search)
    rag = _yes_no_flag(args.rag, "Use local document RAG for context", base.use_rag)
    return apply_patch(base, {"use_web_search": web, "use_rag": rag})


def progress_printer(event: ProgressEvent) -> None:
    icon = {
        STATUS_REFINED: "ok",
        STATUS_SKIPPED: "skip",
        STATUS_FAILED: "FAIL",
        STATUS_UPLOADED: "up",
    }
    detail = f" - {event.message}" if event.message else ""
    print(
        f"  [{event.index}/{event.total}] {icon.get(event.status, event.status):<4} "
        f"{event.title}{detail}"
    )


def _print_summary(summary: PipelineSummary, out_path: Path) -> None:
    print(
        f"\nRefined {summary.refined}, skipped {summary.skipped}, failed {len(summary.failed)} "
        f"of {summary.total}. Tokens: {summary.usage.input_tokens} in / "
        f"{summary.usage.output_tokens} out."
    )
    for key, error in summary.failed:
        print(f"  failed {key}: {error}")
    print(f"Output: {out_path}")


def maybe_upload(
    args: argparse.Namespace, session: ClientSession, out_path: Path, source_audit: str | None
) -> int:
    # --yes means "no questions": without an explicit --upload that is a refine-only run.
    want_upload = False if args.upload is None and args.yes else args.upload
    if not _yes_no_flag(want_upload, "Upload to PwnDoc?", False):
        return 0
    client = session.get()
    target = choose_audit(
        client, args.target_audit or source_audit, "Number of the audit to update"
    )
    count = len(read_findings(out_path))
    if not args.yes and not ask_yes_no(
        f"Update matching findings of audit {target} with {count} refined finding(s)", False
    ):
        print("Upload skipped.")
        return 0
    ok, failed = upload_file(out_path, client, target, progress_printer)
    print(f"\nUploaded {len(ok)}, failed/skipped {len(failed)}.")
    return EXIT_PARTIAL if failed else 0


def cmd_refine(args: argparse.Namespace) -> int:
    store = ConfigStore(args.data_dir)
    session = ClientSession(store.settings, args.username, args.totp)
    try:
        in_path, source_audit = resolve_source(args, session, store.data_dir)
        out_path = resolve_output(args, in_path)
        fields = resolve_fields(args)
        mode = args.mode or ask_choice("How should fields be updated?", MODES)
        settings = resolve_settings(args, store.settings)
        refiner = make_refiner(settings, store.data_dir)
        summary = refine_file(
            in_path, out_path, refiner, make_options(fields, mode), progress_printer
        )
        _print_summary(summary, out_path)
        upload_code = (
            maybe_upload(args, session, out_path, source_audit)
            if summary.refined or summary.skipped
            else 0
        )
    finally:
        session.close()
    return EXIT_PARTIAL if summary.failed or upload_code else 0


# -- other commands -----------------------------------------------------------------------------
def cmd_upload(args: argparse.Namespace) -> int:
    store = ConfigStore(args.data_dir)
    session = ClientSession(store.settings, args.username, args.totp)
    try:
        path = _validated_findings_path(args.file)
        client = session.get()
        target = choose_audit(client, args.audit, "Number of the audit to update")
        ok, failed = upload_file(path, client, target, progress_printer)
    finally:
        session.close()
    print(f"\nUploaded {len(ok)}, failed/skipped {len(failed)}.")
    return EXIT_PARTIAL if failed else 0


def cmd_export(args: argparse.Namespace) -> int:
    store = ConfigStore(args.data_dir)
    session = ClientSession(store.settings, args.username, args.totp)
    try:
        content = session.get().export_docx(args.audit)
    finally:
        session.close()
    Path(args.out).write_bytes(content)
    print(f"Report written to {args.out}")
    return 0


def _parse_assignment(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise CliError(f"--set expects key=value, got {text!r}")
    key = key.strip()
    if key == "llm_api_key" and value == "-":
        value = getpass.getpass("LLM API key: ")
    elif key == "llm_api_key":
        print(
            "warning: an API key given on the command line stays in your shell history and is "
            "visible to other local users; prefer `--set llm_api_key=-` or the LLM_API_KEY "
            "environment variable.",
            file=sys.stderr,
        )
    return key, value


def cmd_config(args: argparse.Namespace) -> int:
    store = ConfigStore(args.data_dir)
    if args.set:
        store.update(dict(_parse_assignment(item) for item in args.set))
    print(json.dumps(store.settings.public(), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    _run_server(args.host, args.port, args.data_dir, args.https, args.insecure_http)
    return 0


# -- parser / entry point -----------------------------------------------------------------------
def _add_login_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--username",
        help="PwnDoc user (or env PWNDOC_USERNAME). Password: env PWNDOC_PASSWORD or prompt.",
    )
    parser.add_argument("--totp", help="TOTP code if the account uses 2FA")


def _add_serve_parser(sub: Any) -> None:
    serve = sub.add_parser("serve", help="run the LAN gateway (PwnDoc UI + AI button)")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - LAN access is the purpose
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--https", action="store_true", help="serve HTTPS with a self-signed certificate"
    )
    serve.add_argument(
        "--insecure-http",
        action="store_true",
        help="allow plain HTTP on a non-loopback address (logins and the API key are then "
        "visible on the network; also settable with PWNDOC_AI_ALLOW_HTTP=1)",
    )
    serve.set_defaults(func=cmd_serve)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pwndoc-ai", description="AI report assistant for PwnDoc")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--data-dir", help="settings/data directory (env PWNDOC_AI_DATA_DIR, default ./data)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    _add_serve_parser(sub)

    refine = sub.add_parser(
        "refine", help="interactive wizard: pick findings, refine with the LLM, upload"
    )
    _add_login_flags(refine)
    refine.add_argument("--source", choices=[k for k, _ in SOURCES])
    refine.add_argument("--audit", help="source audit id")
    refine.add_argument("--input", help="findings JSON file")
    refine.add_argument("--output", help="refined JSON file (resumed if it exists)")
    refine.add_argument("--fields", help=f"comma list from: {', '.join(ALL_FIELDS)} (default all)")
    refine.add_argument("--mode", choices=[k for k, _ in MODES])
    refine.add_argument("--web", action=argparse.BooleanOptionalAction, default=None)
    refine.add_argument("--rag", action=argparse.BooleanOptionalAction, default=None)
    refine.add_argument("--upload", action=argparse.BooleanOptionalAction, default=None)
    refine.add_argument("--target-audit", help="audit to upload to (default: the source audit)")
    refine.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    refine.set_defaults(func=cmd_refine)

    upload = sub.add_parser("upload", help="upload a refined findings JSON file to an audit")
    _add_login_flags(upload)
    upload.add_argument("--file", required=True)
    upload.add_argument("--audit")
    upload.set_defaults(func=cmd_upload)

    config = sub.add_parser("config", help="show or change settings (shared with the web UI)")
    config.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="use llm_api_key=- to be prompted"
    )
    config.set_defaults(func=cmd_config)

    export = sub.add_parser("export", help="download an audit's DOCX report")
    _add_login_flags(export)
    export.add_argument("--audit", required=True)
    export.add_argument("--out", required=True)
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    try:
        return int(args.func(args))
    except (
        CliError,
        PwnDocError,
        LlmError,
        JsonFileError,
        SettingsError,
        TlsUnavailable,
        RagUnavailable,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except EOFError:
        print("error: input closed before the wizard finished", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
