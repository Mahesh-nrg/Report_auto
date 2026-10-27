"""Run the PDF -> DOCX worker in an isolated subprocess and apply the ``auto`` engine rules.

Isolation (docs/EXPORT_DESIGN.md (g)): ``unshare -rn`` when the host allows it, ``ulimit -v``
(512 MB structure, 3 GB pdf2docx) and ``ulimit -t 120`` set by ``/bin/sh`` before ``exec``, a
180 s wall clock, its own session so a timeout kills the whole group, and a minimal
environment. The worker's JSON report is validated as hostile input and its DOCX is rechecked
before it is returned.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import zipfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..export_errors import ExportFailure, ExportInputError, ExportLimitError
from . import limits
from .engines import pdf2docx_present, tools_present
from .errors import PdfConversionFailed, PdfEngineUnavailable, PdfRefused
from .model import ConversionReport, engine_choice, keep_pdf2docx, structure_weakness

log = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).with_name("worker.py")
MEMORY_MB = {"structure": limits.STRUCTURE_MEMORY_MB, "pdf2docx": limits.PDF2DOCX_MEMORY_MB}
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "OPENCV_FOR_THREADS_NUM")
ERRORS: dict[str, type[ExportFailure]] = {
    "refused": PdfRefused,
    "limit": ExportLimitError,
    "unavailable": PdfEngineUnavailable,
    "failed": PdfConversionFailed,
}


@dataclass(frozen=True)
class WorkerResult:
    docx: bytes
    report: ConversionReport


def _isolation() -> list[str]:
    from .. import pdf as renderer

    return renderer._network_isolation()


def worker_command(engine: str, src: Path, out: Path, report: Path, worker: Path) -> list[str]:
    limits_sh = (
        f"ulimit -v {MEMORY_MB[engine] * 1024} 2>/dev/null; "
        f"ulimit -t {limits.WORKER_CPU_SECONDS} 2>/dev/null; "
        'exec "$@"'
    )
    return [
        *_isolation(), "/bin/sh", "-c", limits_sh, "sh",
        sys.executable, "-I", str(worker), "--engine", engine, str(src), str(out), str(report),
    ]  # fmt: skip


def environment(work: Path) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "LANG": "C.UTF-8"}
    env.update({name: "1" for name in THREAD_ENV})
    return env


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.communicate(timeout=limits.REAP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_command(command: list[str], work: Path, timeout: float) -> int:
    """Exit status of the worker; ``PdfConversionFailed('timeout')`` after ``timeout``."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell input from users
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment(work),
            cwd=work,
            start_new_session=True,
        )
    except OSError:
        raise PdfConversionFailed("failed") from None
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise PdfConversionFailed("timeout") from None
    except BaseException:
        _kill_group(process)
        raise
    return process.returncode


def _payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > limits.MAX_REPORT_BYTES:
            raise ValueError("report too large")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise PdfConversionFailed("bad_output") from None
    if not isinstance(data, dict):
        raise PdfConversionFailed("bad_output")
    return data


def _result(payload: dict[str, Any], out: Path) -> WorkerResult:
    if payload.get("ok") is not True:
        kind, code = payload.get("error"), payload.get("code")
        error = ERRORS.get(kind if isinstance(kind, str) else "", PdfConversionFailed)
        raise error(code if isinstance(code, str) else None)
    try:
        report = ConversionReport.from_json(payload.get("report"))
        data = out.read_bytes()
    except (ExportInputError, OSError):
        raise PdfConversionFailed("bad_output") from None
    return WorkerResult(data, report)


def run_worker(
    engine: str, src: Path, work: Path, timeout: float, worker: Path | None = None
) -> WorkerResult:
    """One engine in one isolated worker process; the DOCX is checked before it is returned."""
    folder = work / engine
    folder.mkdir(parents=True, exist_ok=True)
    out, report = folder / "out.docx", folder / "report.json"
    command = worker_command(engine, src, out, report, worker or WORKER_PATH)
    status = run_command(command, folder, timeout)
    if status < 0 or not report.is_file():
        log.warning("PDF import worker stopped (status %s)", status)
        raise PdfConversionFailed("crashed")
    result = _result(_payload(report), out)
    check_output(result.docx, folder)
    return result


def check_output(data: bytes, folder: Path) -> None:
    """The produced DOCX must reopen, pass the upload and renderer checks and stay in caps."""
    from ..docx_store import precheck_docx
    from ..pdf import check_document

    if not data or len(data) > limits.MAX_OUTPUT_BYTES:
        raise PdfConversionFailed("bad_output")
    path = folder / "checked.docx"
    try:
        import docx

        path.write_bytes(data)
        precheck_docx(path)
        check_document(data)
        docx.Document(io.BytesIO(data))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            media = [n for n in archive.namelist() if n.startswith("word/media/")]
    except Exception:  # noqa: BLE001 - any failure here means the output is not usable
        raise PdfConversionFailed("bad_output") from None
    if len(media) > limits.MAX_IMAGES:
        raise PdfConversionFailed("bad_output")


def _auto(src: Path, work: Path, timeout: float) -> WorkerResult:
    structure: WorkerResult | None = None
    try:
        structure = run_worker("structure", src, work, timeout)
        weakness = structure_weakness(structure.report.metrics)
    except PdfConversionFailed:
        weakness = "structure_failed"
    if not weakness and structure is not None:
        return _as_auto(structure, "structure_ok")
    if not pdf2docx_present():
        if structure is None:
            raise PdfConversionFailed("failed")
        return _as_auto(structure, "fallback_unavailable", ("fallback_unavailable",))
    try:
        other = run_worker("pdf2docx", src, work, timeout)
    except (PdfConversionFailed, PdfEngineUnavailable):
        if structure is None:
            raise
        return _as_auto(structure, "pdf2docx_worse")
    before = structure.report.metrics if structure else None
    if keep_pdf2docx(before, other.report.metrics) or structure is None:
        return _as_auto(other, weakness)
    return _as_auto(structure, "pdf2docx_worse")


def _as_auto(result: WorkerResult, reason: str, extra: tuple[str, ...] = ()) -> WorkerResult:
    warnings = tuple(dict.fromkeys((*result.report.warnings, *extra)))
    report = replace(result.report, engine_requested="auto", reason=reason, warnings=warnings)
    return WorkerResult(result.docx, report)


def convert_pdf(
    src: Path, work: Path, engine: str = "auto", *, timeout: float = limits.WORKER_TIMEOUT
) -> tuple[bytes, ConversionReport]:
    """Convert ``src`` into a DOCX inside ``work`` (the caller owns and removes ``work``)."""
    chosen = engine_choice(engine)
    if not tools_present():
        raise PdfEngineUnavailable("pdf_tools")
    if chosen == "pdf2docx" and not pdf2docx_present():
        raise PdfEngineUnavailable("pdf2docx")
    src, work = src.resolve(), work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if chosen == "auto":
        result = _auto(src, work, timeout)
    else:
        result = run_worker(chosen, src, work, timeout)
    return result.docx, result.report


__all__ = [
    "WORKER_PATH",
    "WorkerResult",
    "check_output",
    "convert_pdf",
    "environment",
    "run_command",
    "run_worker",
    "worker_command",
]
