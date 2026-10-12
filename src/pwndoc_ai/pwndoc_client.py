"""Thin client for the PwnDoc REST API (https://pwndoc.github.io/pwndoc/api/).

Two ways to authenticate:
* ``login()`` with username/password (CLI mode) - the JWT cookie is kept in the client's jar.
* ``cookies=`` forwarding the browser's own ``token`` cookie (gateway mode), so the AI acts with
  exactly the permissions of the signed-in PwnDoc user and no credentials are stored.
"""

from __future__ import annotations

from typing import Any

import httpx

EXPORT_TIMEOUT = 120.0  # DOCX generation for large audits is slow


class PwnDocError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PwnDocClient:
    def __init__(
        self,
        base_url: str,
        verify_tls: bool = False,
        cookies: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"Referer": f"{self.base_url}/"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=verify_tls,
            transport=transport,
            timeout=timeout,
        )

    def __enter__(self) -> PwnDocClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- plumbing ---------------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise PwnDocError(f"cannot reach PwnDoc at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise PwnDocError(self._error_text(response), response.status_code)
        if "application/json" in response.headers.get("content-type", ""):
            try:
                body = response.json()
            except ValueError as exc:
                raise PwnDocError("PwnDoc returned invalid JSON", response.status_code) from exc
            if not isinstance(body, dict):
                raise PwnDocError("PwnDoc returned an unexpected JSON shape", response.status_code)
            return body.get("datas")
        return response.content

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            body = response.json()
            detail = body.get("datas", body)
            if isinstance(detail, dict):
                detail = detail.get("msg") or detail.get("message") or detail
            return f"PwnDoc {response.status_code}: {detail}"
        except ValueError:
            return f"PwnDoc {response.status_code}"

    # -- users ------------------------------------------------------------------------------
    def login(self, username: str, password: str, totp: str = "") -> None:
        self._request(
            "POST",
            "/api/users/token",
            json={"username": username, "password": password, "totpToken": totp},
        )

    def me(self) -> dict[str, Any]:
        data = self._request("GET", "/api/users/me")
        if not isinstance(data, dict):
            raise PwnDocError("unexpected response from /api/users/me (is the PwnDoc URL right?)")
        return data

    # -- audits -----------------------------------------------------------------------------
    def list_audits(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/audits") or []

    def get_audit(self, audit_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/audits/{audit_id}")

    def list_findings(self, audit_id: str) -> list[dict[str, Any]]:
        return self.get_audit(audit_id).get("findings", [])

    def update_finding(self, audit_id: str, finding_id: str, payload: dict[str, Any]) -> Any:
        body = {k: v for k, v in payload.items() if k != "_id"}
        return self._request("PUT", f"/api/audits/{audit_id}/findings/{finding_id}", json=body)

    def export_docx(self, audit_id: str) -> bytes:
        return self._request("GET", f"/api/audits/{audit_id}/generate", timeout=EXPORT_TIMEOUT)
