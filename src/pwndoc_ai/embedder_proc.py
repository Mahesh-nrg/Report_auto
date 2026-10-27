"""Process and socket helpers of the on-demand Ollama server (Linux ``/proc``, plain POSIX).

Kept apart from :mod:`pwndoc_ai.embedder` and imported there under the same names, so tests (and
callers) can replace any of them on the embedder module. Nothing here starts or stops a server.
"""

from __future__ import annotations

import os
import socket
import subprocess  # noqa: S404 - only types and signals; the embedder runs the process
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

RESERVED_PORTS = frozenset({11434, 11435})
TCP_LISTEN = "0A"  # the state column of /proc/net/tcp for a listening socket
START_TIME_FIELD = 19  # field 22 of /proc/<pid>/stat counted from the one after the command
PPID_FIELD = 1


def _free_port() -> int:
    """A free loopback port chosen by the OS, never one of Ollama's well-known ones."""
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in RESERVED_PORTS:
            return port


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the server's process group; only called while the leader is still unreaped."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name (state is the first)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None
    return raw.rpartition(")")[2].split()


def _process_start(pid: int) -> int | None:
    """When the process started (clock ticks since boot): tells a pid from its later reuse."""
    fields = _stat_fields(pid)
    try:
        return int(fields[START_TIME_FIELD]) if fields else None
    except (IndexError, ValueError):
        return None


def _process_ppid(pid: int) -> int | None:
    fields = _stat_fields(pid)
    try:
        return int(fields[PPID_FIELD]) if fields else None
    except (IndexError, ValueError):
        return None


def _listener_inodes(port: int) -> set[str]:
    """Inodes of every listening TCP socket bound to ``port`` (``OSError``: no /proc)."""
    found: set[str] = set()
    readable = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for line in lines:
            cols = line.split()
            if (
                len(cols) > 9
                and cols[3] == TCP_LISTEN
                and cols[1].rpartition(":")[2] == f"{port:04X}"
            ):
                found.add(cols[9])
    if not readable:
        raise OSError("no socket table")
    return found


def _group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _stat_fields(int(entry.name))
            if fields and len(fields) > 2 and fields[2] == str(pgid):
                members.append(int(entry.name))
    return members


def _socket_inodes(pid: int) -> set[str] | None:
    """Inodes of the sockets a process holds open; ``None`` when its descriptors are unreadable."""
    base = Path(f"/proc/{pid}/fd")
    try:
        names = os.listdir(base)
    except OSError:
        return None
    inodes: set[str] = set()
    for name in names:
        with suppress(OSError):
            target = os.readlink(base / name)
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    return inodes


def _port_is_ours(process: subprocess.Popen[bytes], port: int) -> bool | None:
    """Whether every listener on ``port`` belongs to the server we started (its process group).

    An HTTP answer alone proves nothing: another process may have taken the port between choosing
    it and the server binding it. ``None`` means this system cannot tell (no ``/proc``).
    """
    try:
        listeners = _listener_inodes(port)
    except OSError:
        return None
    if _socket_inodes(process.pid) is None:
        return None if os.path.isdir(f"/proc/{process.pid}") else False
    if not listeners:
        return False
    owned: set[str] = set()
    for member in _group_members(process.pid):
        owned |= _socket_inodes(member) or set()
    return listeners <= owned


def _kill_pid_group(pid: int, sig: int) -> None:
    """Signal the group of a server that is not our child (only if it leads its own group)."""
    with suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)


class _Record(NamedTuple):
    """One line of a pid file: a server, when it started, its owner and when that one started."""

    pid: int
    start: int = 0
    owner: int = 0
    owner_start: int = 0


def _read_record(path: Path) -> _Record | None:
    """The record in a pid file (also the old layout: the bare pid), ``None`` if unusable."""
    try:
        numbers = [int(part) for part in path.read_text().split()[:4]]
    except (OSError, ValueError):
        return None
    if not numbers or not 1 < numbers[0] < 2**31 or any(n < 0 for n in numbers):
        return None
    return _Record(*numbers)
