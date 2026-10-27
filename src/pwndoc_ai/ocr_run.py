"""Run the OCR tools (pdftoppm, tesseract) under limits, so a hostile PDF cannot exhaust the host.

:func:`run_limited` is the default runner of :func:`pwndoc_ai.rag.ocr_text`. A tool gets resource
limits (address space, largest file, CPU time, no core file) and its own process group, and the
size of the directory it writes into is watched while it runs: past the budget the whole group is
killed. The limits are set by a tiny launcher that applies them and then executes the tool, so
they hold from the tool's first instruction (a ``preexec_fn`` is not safe in a threaded process
such as the gateway, and limits set after the start leave a window in which a fast tool is free).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # noqa: S404 - only runs fixed OCR tools with argv lists
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # not a Unix: the OCR tools then run without resource limits
    resource = None  # type: ignore[assignment]

OCR_MAX_MEMORY_BYTES = 4 * 1024**3  # address space of one OCR tool process
OCR_MAX_FILE_BYTES = 128 * 1024 * 1024  # the largest single file an OCR tool may write
OCR_WATCH_SECONDS = 0.2  # how often the size of the OCR temp directory is checked
CPU_GRACE_SECONDS = 5


class DiskBudgetExceeded(RuntimeError):
    """An OCR tool wrote more into its temp directory than the budget allows and was stopped."""


def _dir_bytes(folder: Path) -> int:
    total = 0
    with suppress(OSError), os.scandir(folder) as entries:
        for entry in entries:
            with suppress(OSError):
                total += entry.stat(follow_symlinks=False).st_size
    return total


_LAUNCHER = (
    "import json, os, resource, sys\n"
    "for name, value in json.loads(sys.argv[1]).items():\n"
    "    try:\n"
    "        resource.setrlimit(getattr(resource, name), (value, value))\n"
    "    except (ValueError, OSError):\n"
    "        pass\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


def _limited_argv(argv: list[str], timeout: float) -> list[str]:
    """``argv`` behind a launcher that caps memory, file size and CPU time and then execs it."""
    if resource is None:
        return argv
    limits = {
        "RLIMIT_AS": OCR_MAX_MEMORY_BYTES,
        "RLIMIT_FSIZE": OCR_MAX_FILE_BYTES,
        "RLIMIT_CORE": 0,
        "RLIMIT_CPU": int(timeout) + CPU_GRACE_SECONDS,
    }
    return [sys.executable, "-I", "-c", _LAUNCHER, json.dumps(limits), *argv]


def _kill_group(process: subprocess.Popen[Any]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)  # it leads its own session (start_new_session)
    process.wait()


def _wait(
    process: subprocess.Popen[Any],
    argv: list[str],
    timeout: float,
    watch_dir: Path | None,
    max_dir_bytes: int | None,
) -> tuple[Any, Any]:
    """The tool's output once it ends; stops it (and raises) on a timeout or a full directory."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        try:
            return process.communicate(timeout=min(OCR_WATCH_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            if watch_dir is not None and max_dir_bytes and _dir_bytes(watch_dir) > max_dir_bytes:
                raise DiskBudgetExceeded("the OCR temp directory outgrew its budget") from None


def run_limited(
    argv: list[str],
    *,
    timeout: float,
    capture_output: bool = True,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    check: bool = False,
    watch_dir: Path | None = None,
    max_dir_bytes: int | None = None,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` for an OCR tool, under limits (the default runner of ``ocr_text``).

    The tool gets resource limits (address space, largest file, CPU time, no core file) and its
    own process group. While it runs the size of ``watch_dir`` is checked every
    ``OCR_WATCH_SECONDS``: past ``max_dir_bytes`` the whole group is killed and
    :class:`DiskBudgetExceeded` raised, so the budget bounds the disk use, not just the pages kept.
    A timeout kills the group and raises ``subprocess.TimeoutExpired``.
    """
    pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(  # noqa: S603 - a fixed tool path from which(), argv list, no shell
        _limited_argv(argv, timeout),
        stdin=subprocess.DEVNULL,
        stdout=pipe,
        stderr=pipe,
        text=text,
        encoding=encoding,
        errors=errors,
        start_new_session=True,
    )
    try:
        out, err = _wait(process, argv, timeout, watch_dir, max_dir_bytes)
    except BaseException:
        _kill_group(process)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)
