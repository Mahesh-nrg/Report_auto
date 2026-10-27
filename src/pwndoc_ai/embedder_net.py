"""Network helpers of the embedder: the guarded transport for an external Ollama, and a check.

An external Ollama is an address an administrator typed in. Each request to it resolves the host
once, refuses link-local and cloud-metadata answers (the guard of :mod:`pwndoc_ai.netguard`) and
connects to the address that was checked, keeping the host name for ``Host`` and TLS.
"""

from __future__ import annotations

import ipaddress

import httpx

from .netguard import MAX_PINNED_ADDRESSES, inspect_http_url

_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home", ".localdomain")


def valid_url(value: str) -> bool:
    """Whether ``value`` is an http(s) URL that does not point at link-local / metadata space."""
    try:
        inspect_http_url(value, "Ollama URL", resolve=False)
    except ValueError:
        return False
    return True


class _GuardedTransport(httpx.BaseTransport):
    """Sends each request to the address that was just checked (no second lookup, no metadata).

    Used for an external server the admin named: its host name is resolved here, every answer
    must pass the link-local / cloud-metadata guard, and the connection goes to a checked
    address with the real host name kept in the ``Host`` header and the TLS name.
    """

    def __init__(self, inner: httpx.BaseTransport | None = None):
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            target = inspect_http_url(str(request.url), "Ollama URL")
        except ValueError:
            raise httpx.ConnectError("the Ollama address is not allowed", request=request) from None
        host = request.url.host
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": host}
        failure: httpx.ConnectError | None = None
        for address in (target.addresses or (host,))[:MAX_PINNED_ADDRESSES]:
            request.url = request.url.copy_with(host=address)
            try:
                return self._inner.handle_request(request)
            except httpx.ConnectError as exc:
                failure = exc
        raise failure or httpx.ConnectError("no address to connect to", request=request)

    def close(self) -> None:
        self._inner.close()


def is_external_url(url: str) -> bool:
    """Whether ``url`` names a server outside this machine and its local network."""
    if not url:
        return False
    host = (httpx.URL(url).host or "").lower() if valid_url(url) else ""
    if not host or host == "localhost" or "." not in host and ":" not in host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        answers = [ipaddress.ip_address(a) for a in _resolve_host(host)]
    except ValueError:
        return True
    return any(address.is_global for address in answers)


def _resolve_host(host: str) -> list[str]:
    from .netguard import _resolve

    return _resolve(host) or []
