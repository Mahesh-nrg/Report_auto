"""Small local embedder served by Ollama, running only while something needs an embedding.

Every embedding of the application (documents being indexed, searches of the local index, web
search results that are ranked) goes through this one class. ``ollama serve`` is started the
first time an embedding is needed and stopped again after ``idle_seconds`` without use. It is
started on a random free loopback port of its own (never 11434), with a scrubbed environment, in
its own process group so that stopping it takes its model runner along.

An ``ollama_url`` in the settings names an EXTERNAL server instead: it is used as it is, never
started and never stopped.

Configuration comes from the settings (``embed_backend``, ``ollama_model``, ``ollama_url``,
``ollama_idle_seconds``); these environment variables win over them:

* ``PWNDOC_AI_EMBEDDER``     ``auto`` (Ollama when it is installed or reachable, else the built-in
                             ONNX embedder), ``ollama`` or ``onnx``
* ``PWNDOC_AI_OLLAMA``       path of the ``ollama`` binary (default ``<data dir>/../.ollama/bin``,
                             then ``ollama`` on the PATH)
* ``PWNDOC_AI_OLLAMA_URL``   an external server (default: start our own)
* ``PWNDOC_AI_OLLAMA_MODEL`` default ``all-minilm`` (about 45 MB)
* ``PWNDOC_AI_OLLAMA_IDLE``  seconds without use before our server is stopped (30 to 3600)

Vectors of ``all-minilm`` are the same vectors as ChromaDB's built-in ONNX embedder produces
(vector space ``minilm-l6-v2``), so an index built with one can be searched with the other.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess  # noqa: S404 - runs the configured ollama binary with a fixed argv, no shell
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .embedder_net import (  # noqa: F401 - re-exported: tests and callers use them from here
    _GuardedTransport,
    is_external_url,
    valid_url,
)
from .embedder_proc import (  # noqa: F401 - the process helpers are replaced through this module
    RESERVED_PORTS,
    _cmdline,
    _free_port,
    _kill_pid_group,
    _pid_alive,
    _port_is_ours,
    _process_ppid,
    _process_start,
    _read_record,
    _Record,
    _signal_group,
)
from .embedder_space import MINILM_SPACE, vector_space  # noqa: F401 - part of this module's API
from .netguard import inspect_http_url

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

DEFAULT_MODEL = "all-minilm"
DEFAULT_IDLE_SECONDS = 300.0
MIN_IDLE_SECONDS = 30.0
MAX_IDLE_SECONDS = 3600.0
START_TIMEOUT = 60.0  # seconds for a fresh ollama to answer
START_ATTEMPTS = 3  # a port chosen a moment ago may have been taken since
POLL_INTERVAL = 0.2
PING_TIMEOUT = 1.0
REQUEST_TIMEOUT = 300.0  # one embedding batch: the first one loads the model on a CPU
PULL_TIMEOUT = 900.0  # the whole model download
PULL_READ_TIMEOUT = 120.0  # silence between two progress lines
STOP_WAIT = 10.0
ORPHAN_WAIT = 5.0
BATCH_SIZE = 16
MAX_TEXT_CHARS = 4000  # a longer text is cut: the model only reads its first few hundred tokens
MAX_CALL_CHARS = 2_000_000
MAX_DIMENSIONS = 8192
MODES = ("auto", "ollama", "onnx")
BINARY_ENV = "PWNDOC_AI_OLLAMA"
DEFAULT_REGISTRY = "registry.ollama.ai"  # the only registry a model is ever pulled from
PID_FILE_GLOB = "serve*.pid"  # ``serve.<pid>.pid`` per server, and the one-file layout it replaced
_MODEL_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BAD_RESPONSE = "Ollama answered with an unexpected embedding response"
_LIVE: weakref.WeakSet[OllamaEmbedder] = weakref.WeakSet()


class EmbedderError(RuntimeError):
    """The embedder cannot be started or answered (the text is safe to show to an admin)."""


class _EarlyExit(EmbedderError):
    """The server we just started ended at once (most likely its port was taken)."""


@dataclass(frozen=True)
class OllamaConfig:
    url: str = ""  # "" = start our own server; otherwise an external one that is never stopped
    model: str = DEFAULT_MODEL
    binary: Path | None = None
    models_dir: Path | None = None
    home: Path | None = None  # HOME of the server process, its log file and its pid file
    idle_seconds: float = DEFAULT_IDLE_SECONDS


def model_parts(model: str) -> tuple[str, str, str, str]:
    """``(host, namespace, name, tag)`` of a model reference.

    Refuses anything path-like and any registry but the default one: a model of another host
    would make the server pull and load a file from wherever the name points.
    """
    head, _, tag = model.partition(":")
    segments = head.split("/")
    parts = [*([DEFAULT_REGISTRY, "library"][: 3 - len(segments)]), *segments]
    valid = len(segments) <= 3 and len(parts) == 3 and all(map(_MODEL_PART.fullmatch, parts))
    if not valid or (tag and not _MODEL_PART.fullmatch(tag)) or ":" in tag:
        raise EmbedderError("the Ollama model name is not valid")
    if parts[0] != DEFAULT_REGISTRY:
        raise EmbedderError("only models of the default Ollama registry can be used")
    return parts[0], parts[1], parts[2], tag or "latest"


def _number(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _idle_seconds(value: float) -> float:
    return min(MAX_IDLE_SECONDS, max(MIN_IDLE_SECONDS, value))


def find_binary(data_dir: Path, env: Mapping[str, str]) -> Path | None:
    """The ollama binary: the configured one, the project's ``.ollama``, or the one on the PATH."""
    configured = env.get(BINARY_ENV, "").strip()
    local = data_dir.resolve().parent / ".ollama" / "bin" / "ollama"
    candidates = [Path(configured)] if configured else [local]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = None if configured else shutil.which("ollama", path=env.get("PATH"))
    return Path(found) if found else None


def _models_dir(binary: Path | None, data_dir: Path, env: Mapping[str, str]) -> Path:
    if env.get("OLLAMA_MODELS"):
        return Path(env["OLLAMA_MODELS"])
    if binary is not None and (binary.parent.parent / "models").is_dir():
        return binary.parent.parent / "models"  # the project's own ``.ollama`` layout
    return data_dir / "ollama" / "models"


def _from(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, None) if settings is not None else None
    return default if value is None else value


def _valid_model(value: str) -> bool:
    try:
        model_parts(value)
    except EmbedderError:
        return False
    return True


def _env_choice(env: Mapping[str, str], name: str, valid: Callable[[str], bool]) -> str:
    """The value of ``name`` when it is usable; an unusable one is ignored (with a warning)."""
    value = env.get(name, "").strip()
    if value and not valid(value):
        log.warning("%s is not valid and is ignored; the stored setting is used", name)
        return ""
    return value


def load_config(
    data_dir: str | Path, env: Mapping[str, str] | None = None, settings: Settings | None = None
) -> OllamaConfig:
    """The embedder settings: a valid environment value first, then ``settings``, then defaults."""
    env = os.environ if env is None else env
    base = Path(data_dir)
    binary = find_binary(base, env)
    configured_idle = _number(str(_from(settings, "ollama_idle_seconds", "")), DEFAULT_IDLE_SECONDS)
    url = _env_choice(env, "PWNDOC_AI_OLLAMA_URL", valid_url)
    url = url or str(_from(settings, "ollama_url", ""))
    model = (
        _env_choice(env, "PWNDOC_AI_OLLAMA_MODEL", _valid_model)
        or str(_from(settings, "ollama_model", DEFAULT_MODEL)).strip()
        or DEFAULT_MODEL
    )
    return OllamaConfig(
        url=url.strip().rstrip("/"),
        model=model,
        binary=binary,
        models_dir=_models_dir(binary, base, env),
        home=base / "rag" / "ollama-home",
        idle_seconds=_idle_seconds(_number(env.get("PWNDOC_AI_OLLAMA_IDLE"), configured_idle)),
    )


def _close_all() -> None:
    """The exit hook: no server started by this interpreter outlives it."""
    for embedder in list(_LIVE):
        try:
            embedder.close()
        except Exception:  # noqa: BLE001 - exiting: nothing useful to do about it
            log.warning("could not stop the Ollama embedder at exit")


atexit.register(_close_all)


def _checked_texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, str) or not all(isinstance(text, str) for text in texts):
        raise EmbedderError("only a list of text strings can be embedded")
    cut = [text[:MAX_TEXT_CHARS] for text in texts]
    if sum(len(text) for text in cut) > MAX_CALL_CHARS:
        raise EmbedderError("too much text to embed in one call")
    return cut


def _validated(data: Any, expected: int, dimensions: int | None) -> list[list[float]]:
    vectors = data.get("embeddings") if isinstance(data, dict) else None
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbedderError(_BAD_RESPONSE)
    width = dimensions if dimensions is not None else (len(vectors[0]) if vectors else 0)
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != width or not 0 < width <= MAX_DIMENSIONS:
            raise EmbedderError(_BAD_RESPONSE)
        if not all(type(x) in (int, float) and math.isfinite(x) for x in vector):
            raise EmbedderError(_BAD_RESPONSE)
    return [[float(x) for x in vector] for vector in vectors]


class OllamaEmbedder:
    """Embeds texts through a lazily started ``ollama serve`` (or an external one)."""

    def __init__(
        self,
        config: OllamaConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        pick_port: Callable[[], int] = _free_port,
        owns_port: Callable[[subprocess.Popen[bytes], int], bool | None] = _port_is_ours,
    ):
        model_parts(config.model)  # a model name becomes part of a path: refuse odd ones now
        self.config = config
        self._transport = transport
        self._spawn = spawn
        self._pick_port = pick_port
        self._owns_port = owns_port
        self._lock = threading.RLock()  # short sections: the fields below
        self._start_lock = threading.Lock()  # one start / model check / download at a time
        self._process: subprocess.Popen[bytes] | None = None  # only a server we started
        self._url = config.url  # the address in use ("" until our own server is up)
        self._model_ready = False
        self._dimensions: int | None = None
        self._inflight = 0
        self._generation = 0  # bumped by every use: a timer of an older one is stale
        self._epoch = 0  # bumped by close(): a start that began earlier gives up
        self._timer: threading.Timer | None = None
        self._pull: dict[str, Any] | None = None
        self._validate_url()

    @property
    def name(self) -> str:
        return f"ollama:{self.config.model}"

    @property
    def space(self) -> str:
        return vector_space(self.config.model)

    # -- public ---------------------------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text (our server is started first when it is not running)."""
        batch = _checked_texts(texts)
        if not batch:
            return []
        self._begin()
        try:
            self._ensure_ready()
            vectors: list[list[float]] = []
            for start in range(0, len(batch), BATCH_SIZE):
                vectors.extend(self._embed_batch(batch[start : start + BATCH_SIZE]))
            return vectors
        finally:
            self._end()

    def prepare(self) -> None:
        """Start the server if needed and download the model if it is missing (a bounded wait)."""
        self._begin()
        try:
            self._ensure_ready()
        finally:
            self._end()

    def reachable(self) -> bool:
        """Whether an Ollama answers at the address in use right now."""
        url = self._url
        if not url:
            return False
        try:
            with self._client(url, PING_TIMEOUT) as client:
                return client.get("/api/version").status_code == 200
        except httpx.HTTPError:
            return False

    def installed(self) -> bool:
        """Whether this embedder can run: a binary to start, or an external server that answers."""
        return self.config.binary is not None or (bool(self.config.url) and self.reachable())

    @property
    def running(self) -> bool:
        """Whether a server is up: ours alive, or the external one answering."""
        if self.config.url:
            return self.reachable()
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def model_present(self) -> bool:
        """Whether the model is downloaded; asks a running server, else looks at the disk."""
        if self.reachable():
            with suppress(EmbedderError):
                return self._model_listed()
        return self._model_on_disk()

    def pull_progress(self) -> dict[str, Any] | None:
        """``{status, percent}`` of a model download in flight, else ``None``."""
        with self._lock:
            return None if self._pull is None else dict(self._pull)

    def status(self) -> dict[str, Any]:
        """What the settings page shows. Never starts the server."""
        installed, running = self.installed(), self.running
        present = self.model_present()
        return {
            "model": self.config.model,
            "space": self.space,
            "installed": installed,
            "running": running,
            "model_present": present,
            "detail": self._detail(installed, running, present),
        }

    def close(self) -> None:
        """Stop the server this embedder started (an external one is left alone)."""
        with self._lock:
            self._epoch += 1
            self._cancel_timer()
        self._stop()

    # -- status helpers -------------------------------------------------------------------
    def _detail(self, installed: bool, running: bool, present: bool) -> str:
        progress = self.pull_progress()
        if progress is not None:
            return f"downloading the model ({progress.get('percent', 0)}%)"
        if not installed:
            return "Ollama is not installed; the built-in embedder is used"
        if not present:
            return "the model is downloaded on first use (or use Prepare)"
        return "ready" if running else "ready; the server starts when it is needed"

    def _model_on_disk(self) -> bool:
        models = self.config.models_dir
        if models is None:
            return False
        host, namespace, name, tag = model_parts(self.config.model)
        return (models / "manifests" / host / namespace / name / tag).is_file()

    # -- server life cycle ----------------------------------------------------------------
    def _validate_url(self) -> None:
        if not self.config.url:
            return
        try:
            inspect_http_url(self.config.url, "Ollama URL", resolve=False)
        except ValueError as exc:
            raise EmbedderError(str(exc)) from None

    def _client(self, url: str, timeout: float) -> httpx.Client:
        transport = self._transport
        if transport is None and self.config.url:
            transport = _GuardedTransport()  # an external server: only checked addresses
        return httpx.Client(base_url=url, timeout=timeout, transport=transport, trust_env=False)

    def _begin(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._inflight += 1
            self._generation += 1

    def _end(self) -> None:
        with self._lock:
            self._inflight -= 1
            if self._inflight == 0:
                self._schedule_stop()

    def _ensure_ready(self) -> None:
        with self._start_lock:
            with self._lock:
                if self._process is not None and self._process.poll() is not None:
                    self._process, self._model_ready, self._url = None, False, self.config.url
            if self.config.url:
                if not self.reachable():
                    raise EmbedderError("the configured Ollama server is not reachable")
            elif not self.running:
                self._model_ready = False
                self._start()
            if not self._model_ready:
                self._ensure_model()
                self._model_ready = True

    def _start(self) -> None:
        config = self.config
        if config.binary is None or config.home is None:
            raise EmbedderError(
                f"Ollama is not installed here; install it or set {BINARY_ENV} to the ollama binary"
            )
        try:
            config.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise EmbedderError(f"could not prepare Ollama: {exc.strerror or 'I/O error'}") from exc
        self._reap_orphan()
        with self._lock:
            epoch = self._epoch  # a close() from now on ends every attempt below
        for attempt in range(START_ATTEMPTS):
            try:
                self._launch_once(epoch)
                return
            except _EarlyExit:
                if attempt == START_ATTEMPTS - 1:
                    raise EmbedderError(
                        "Ollama exited right after it was started "
                        "(see serve.log in the embedder home folder)"
                    ) from None
                log.info("Ollama exited at once (port taken?); trying another port")

    def _launch_once(self, epoch: int) -> None:
        config = self.config
        assert config.binary is not None and config.home is not None  # noqa: S101 - see _start
        port = self._pick_port()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(config.home),
            "LANG": "C.UTF-8",
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_MODELS": str(config.models_dir),
            "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_NOPRUNE": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
        }
        process = self._spawn_process(env, config.home)
        with self._lock:
            stale = self._epoch != epoch  # close() ran while we were launching
            if not stale:
                self._process, self._url = process, f"http://127.0.0.1:{port}"
                _LIVE.add(self)
        if stale:
            self._terminate(process)
            raise EmbedderError("the embedder was stopped while it was starting")
        self._write_pid(process.pid)
        log.info("started the on-demand Ollama embedder (pid %s)", process.pid)
        self._wait_until_up(process, epoch, port)

    def _spawn_process(self, env: dict[str, str], home: Path) -> subprocess.Popen[bytes]:
        assert self.config.binary is not None  # noqa: S101 - see _start
        try:
            fd = os.open(home / "serve.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as log_file:
                return self._spawn(  # noqa: S603 - fixed argv from a configured path
                    [str(self.config.binary), "serve"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    cwd=home,
                    start_new_session=True,  # so stopping it takes its model runner along
                )
        except OSError as exc:
            raise EmbedderError(
                f"could not start Ollama: {exc.strerror or type(exc).__name__}"
            ) from exc

    def _wait_until_up(self, process: subprocess.Popen[bytes], epoch: int, port: int) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self._epoch != epoch:
                raise EmbedderError("the embedder was stopped while it was starting")
            if process.poll() is not None:
                self._forget(process)
                raise _EarlyExit("Ollama exited right after it was started")
            if self.reachable() and self._accept(process, epoch, port):
                return
            time.sleep(POLL_INTERVAL)
        self._stop()
        raise EmbedderError("Ollama did not become ready in time")

    def _accept(self, process: subprocess.Popen[bytes], epoch: int, port: int) -> bool:
        """Whether the server that answered on ``port`` is the one we started (else: give up it).

        Any process may answer on a port that was free a moment ago, so the answer is not enough:
        the listening socket must belong to our process group, and our server must still be alive.
        """
        owned = self._owns_port(process, port)
        if owned is None:  # cannot tell: it must at least outlive one more poll and still answer
            time.sleep(POLL_INTERVAL)
            owned = process.poll() is None and self.reachable()
        if owned and process.poll() is None:
            return True
        self._terminate(process)  # a stranger holds the port, or ours is gone
        self._forget(process)
        raise _EarlyExit("the port is not served by the Ollama we started")

    def _forget(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process, self._url, self._model_ready = None, self.config.url, False
        self._remove_pid(process.pid)

    def _detach(self) -> subprocess.Popen[bytes] | None:
        """Take our server out of service (call with the lock held); the caller ends it."""
        process, self._process = self._process, None
        self._url, self._model_ready = self.config.url, False
        return process

    def _stop(self) -> None:
        with self._lock:
            process = self._detach()
        self._finish_stop(process)

    def _finish_stop(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        self._terminate(process)
        self._remove_pid(process.pid)
        log.info("stopped the on-demand Ollama embedder")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        _signal_group(process, signal.SIGTERM)  # a graceful stop lets ollama end its model runner
        try:
            process.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            process.wait()

    # -- orphans of a gateway that was killed ---------------------------------------------
    def _pid_path(self, pid: int) -> Path | None:
        return None if self.config.home is None else self.config.home / f"serve.{pid}.pid"

    def _write_pid(self, pid: int) -> None:
        """Record the server, when it started and who owns it, in a file of its own."""
        path = self._pid_path(pid)
        if path is None:
            return
        me = os.getpid()
        fields = (pid, _process_start(pid) or 0, me, _process_start(me) or 0)
        with suppress(OSError):
            path.write_text(" ".join(map(str, fields)) + "\n")

    def _remove_pid(self, pid: int) -> None:
        """Forget one server; the records of other servers in the same folder are not touched."""
        path = self._pid_path(pid)
        if path is not None:
            path.unlink(missing_ok=True)

    def reap_orphan(self) -> None:
        """Stop the servers a gateway that was killed left behind (never an external server)."""
        if not self.config.url and self.config.binary is not None:
            self._reap_orphan()

    def _reap_orphan(self) -> None:
        """Stop the servers earlier runs left behind; a server whose owner is alive is kept."""
        home = self.config.home
        if home is None or not home.is_dir():
            return
        for path in sorted(home.glob(PID_FILE_GLOB)):
            record = _read_record(path)
            if record is None:
                path.unlink(missing_ok=True)  # garbage: nothing to stop
            else:
                self._reap(path, record)

    def _reap(self, path: Path, record: _Record) -> None:
        pid = record.pid
        alive = _pid_alive(pid) and self._is_ours(_cmdline(pid))
        started = _process_start(pid)
        if not alive or (record.start and started and started != record.start):
            path.unlink(missing_ok=True)  # gone, or the pid belongs to some other process now
            return
        if self._has_live_owner(record):
            return  # another embedder (ours, the CLI, a second gateway) is still using it
        path.unlink(missing_ok=True)
        log.warning("stopping an Ollama embedder left behind by an earlier run (pid %s)", pid)
        _kill_pid_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + ORPHAN_WAIT
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_alive(pid):
            _kill_pid_group(pid, signal.SIGKILL)

    @staticmethod
    def _has_live_owner(record: _Record) -> bool:
        """The owner of the server is running and the server is still its child."""
        if not record.owner or not _pid_alive(record.owner):
            return False
        started = _process_start(record.owner)
        if record.owner_start and started and started != record.owner_start:
            return False  # that pid was reused by an unrelated process
        parent = _process_ppid(record.pid)
        return parent is None or parent == record.owner

    def _is_ours(self, argv: list[str]) -> bool:
        binary = str(self.config.binary)
        return bool(argv) and argv[-1] == "serve" and binary in argv[:2]

    # -- idle shutdown --------------------------------------------------------------------
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_stop(self) -> None:
        if self._process is None:
            return  # nothing of ours is running
        generation = self._generation
        self._timer = threading.Timer(
            self.config.idle_seconds, self._stop_if_idle, args=(generation,)
        )
        self._timer.daemon = True
        self._timer.start()

    def _stop_if_idle(self, generation: int) -> None:
        with self._lock:
            if self._inflight != 0 or generation != self._generation:
                return  # used since this timer was set: it is stale
            self._timer = None
            process = self._detach()  # decided and detached under one lock: no use can slip in
        self._finish_stop(process)

    # -- the model ------------------------------------------------------------------------
    def _model_listed(self) -> bool:
        wanted = self.config.model
        try:
            with self._client(self._url, REQUEST_TIMEOUT) as client:
                tags = client.get("/api/tags")
                tags.raise_for_status()
                names = {str(m.get("name", "")) for m in tags.json().get("models", [])}
        except (httpx.HTTPError, ValueError, AttributeError):
            raise EmbedderError("could not list the Ollama models") from None
        return wanted in names or f"{wanted}:latest" in names

    def _ensure_model(self) -> None:
        if self._model_listed():
            return
        log.info("downloading the embedding model %s once", self.config.model)
        try:
            self._pull_model()
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise self._pull_failed(type(exc).__name__) from None
        finally:
            with self._lock:
                self._pull = None
        if not self._model_listed():
            raise self._pull_failed("incomplete")

    def _pull_failed(self, reason: str) -> EmbedderError:
        wanted = self.config.model
        return EmbedderError(
            f"the embedding model {wanted!r} is not installed and could not be downloaded "
            f"({reason}); run: ollama pull {wanted}"
        )

    def _pull_model(self) -> None:
        deadline = time.monotonic() + PULL_TIMEOUT
        timeout = httpx.Timeout(REQUEST_TIMEOUT, read=PULL_READ_TIMEOUT)
        payload = {"model": self.config.model, "stream": True}
        with self._client(self._url, REQUEST_TIMEOUT) as client:
            with client.stream("POST", "/api/pull", json=payload, timeout=timeout) as response:
                response.raise_for_status()
                for raw in response.iter_lines():
                    if time.monotonic() > deadline:
                        raise EmbedderError("the model download is taking too long; try again")
                    if raw.strip():
                        self._record_pull(json.loads(raw))

    def _record_pull(self, line: Any) -> None:
        if not isinstance(line, dict):
            raise ValueError("unexpected download progress")
        if "error" in line:
            raise ValueError("the download was refused")
        total, done = line.get("total"), line.get("completed")
        progress: dict[str, Any] = {"status": str(line.get("status", ""))[:80]}
        if isinstance(total, int | float) and isinstance(done, int | float) and total > 0:
            progress["percent"] = int(min(100, max(0, done * 100 // total)))
            self._log_pull_step(progress["percent"])
        with self._lock:
            self._pull = progress

    def _log_pull_step(self, percent: int) -> None:
        with self._lock:
            last = (self._pull or {}).get("percent", -10)
        if percent // 10 > last // 10:
            log.info("downloading the embedding model: %d%%", percent)

    # -- requests -------------------------------------------------------------------------
    def _check_alive(self) -> None:
        """Our server must still be running when text is about to be sent to its port."""
        with self._lock:
            process = self._process
            if process is None or process.poll() is None:
                return
            self._process, self._model_ready, self._url = None, False, self.config.url
        self._remove_pid(process.pid)
        raise EmbedderError("the Ollama server stopped; try again")

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        self._check_alive()
        payload: dict[str, Any] = {"model": self.config.model, "input": batch, "truncate": True}
        try:
            with self._client(self._url, REQUEST_TIMEOUT) as client:
                response = client.post("/api/embed", json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._model_ready = False  # look at the server and the model again next time
            raise EmbedderError(f"Ollama could not embed the text ({type(exc).__name__})") from None
        vectors = _validated(data, len(batch), self._dimensions)
        with self._lock:
            self._dimensions = len(vectors[0])
        return vectors


def make_embedder(
    data_dir: str | Path,
    settings: Settings | None = None,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OllamaEmbedder | None:
    """The embedder to use, or ``None`` for ChromaDB's built-in ONNX one.

    The backend is ``PWNDOC_AI_EMBEDDER`` or the ``embed_backend`` setting. ``auto`` picks Ollama
    only when it can run (a binary to start or an external server answering); otherwise the
    index keeps using the built-in embedder, whose vectors are the same ones.
    """
    env = os.environ if env is None else env
    wanted = env.get("PWNDOC_AI_EMBEDDER", "").strip().lower()
    if wanted and wanted not in MODES:
        log.warning("PWNDOC_AI_EMBEDDER is not one of %s and is ignored", MODES)
        wanted = ""
    mode = wanted or str(_from(settings, "embed_backend", "auto")).strip().lower()
    if mode not in MODES:
        log.warning("the embedder backend %r is not one of %s; using auto", mode, MODES)
        mode = "auto"
    if mode == "onnx":
        return None
    try:
        embedder = OllamaEmbedder(load_config(data_dir, env, settings), transport=transport)
    except EmbedderError as exc:
        log.warning("Ollama embedder disabled: %s", exc)
        return None
    return embedder if mode == "ollama" or embedder.installed() else None
