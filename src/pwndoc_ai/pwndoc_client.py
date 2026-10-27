"""Thin client for the PwnDoc REST API (https://pwndoc.github.io/pwndoc/api/).

Two ways to authenticate:
* ``login()`` with username/password (CLI mode) - the JWT cookie is kept in the client's jar.
* ``cookies=`` forwarding the browser's own ``token`` cookie (gateway mode), so the AI acts with
  exactly the permissions of the signed-in PwnDoc user and no credentials are stored.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

EXPORT_TIMEOUT = 120.0  # DOCX generation for large audits is slow
LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
# Ids that are put into URL paths: PwnDoc's are 24 hex digits, never anything that could climb
# out of the path or add a query.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OBJECT_ID = re.compile(r"[0-9a-f]{24}")  # what PwnDoc returns for a new object (Mongo ObjectId)
CLIENT_KEYS = ("email", "firstname", "lastname")


def safe_id(value: object, what: str) -> str:
    """``value`` when it is a plain id, else ``ValueError`` (nothing is sent to PwnDoc)."""
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {what} id")
    return value


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
        return self._request("GET", f"/api/audits/{safe_id(audit_id, 'audit')}")

    def list_findings(self, audit_id: str) -> list[dict[str, Any]]:
        return self.get_audit(audit_id).get("findings", [])

    def update_finding(self, audit_id: str, finding_id: str, payload: dict[str, Any]) -> Any:
        path = f"/api/audits/{safe_id(audit_id, 'audit')}/findings/{safe_id(finding_id, 'finding')}"
        body = {k: v for k, v in payload.items() if k != "_id"}
        return self._request("PUT", path, json=body)

    def export_docx(self, audit_id: str) -> bytes:
        path = f"/api/audits/{safe_id(audit_id, 'audit')}/generate"
        return self._request("GET", path, timeout=EXPORT_TIMEOUT)

    def update_section(self, audit_id: str, section_id: str, payload: dict[str, Any]) -> Any:
        path = f"/api/audits/{safe_id(audit_id, 'audit')}/sections/{safe_id(section_id, 'section')}"
        return self._request("PUT", path, json=payload)

    # -- setup data (languages, audit types, custom fields, templates) ----------------------
    def list_languages(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/data/languages") or []

    def create_language(self, language: str, locale: str) -> Any:
        if not language.strip() or not locale.strip():
            raise ValueError("language and locale must both be non-empty")
        body = {"language": language.strip(), "locale": locale.strip()}
        return self._request("POST", "/api/data/languages", json=body)

    def list_audit_types(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/data/audit-types") or []

    def update_audit_types(self, audit_types: list[dict[str, Any]]) -> Any:
        """Replace ALL audit types (PwnDoc's PUT is a full replacement, so send the whole list)."""
        if not isinstance(audit_types, list):
            raise ValueError("audit_types must be a list of audit type objects")
        return self._request("PUT", "/api/data/audit-types", json=audit_types)

    def list_custom_fields(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/data/custom-fields") or []

    def create_custom_field(self, payload: dict[str, Any]) -> Any:
        return self._request("POST", "/api/data/custom-fields", json=payload)

    def list_templates(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/templates") or []

    # -- vulnerability library ----------------------------------------------------------------
    def list_vulnerabilities(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/vulnerabilities") or []

    def list_vulnerabilities_by_locale(self, locale: str) -> list[dict[str, Any]]:
        """Library entries WITH their wording in one language (``{"detail": {...}}`` per item).

        ``list_vulnerabilities`` only returns titles; this is the call that carries the
        description, remediation and references.
        """
        if not isinstance(locale, str) or not LOCALE.match(locale):
            raise ValueError(f"invalid locale: {locale!r}")
        return self._request("GET", f"/api/vulnerabilities/{locale}") or []

    def create_vulnerabilities(self, items: list[dict[str, Any]]) -> Any:
        if not isinstance(items, list):
            raise ValueError("items must be a list of vulnerability objects")
        return self._request("POST", "/api/vulnerabilities", json=items)

    # -- DOCX import: companies, clients, audit creation, findings, images -------------------
    def _object(self, data: Any, what: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise PwnDocError(f"unexpected response for {what}")
        return data

    def _new_id(self, data: Any, what: str) -> str:
        found = data.get("_id") if isinstance(data, dict) else None
        if not isinstance(found, str) or not OBJECT_ID.fullmatch(found):
            raise PwnDocError(f"PwnDoc returned no valid {what} id")
        return found

    def list_companies(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/companies") or []

    def create_company(self, name: str) -> dict[str, Any]:
        """201 ``{_id, name}``; a duplicate name raises ``PwnDocError(status=422)``."""
        data = self._object(self._request("POST", "/api/companies", json={"name": name}), "company")
        self._new_id(data, "company")
        return data

    def delete_company(self, company_id: str) -> None:
        self._request("DELETE", f"/api/companies/{safe_id(company_id, 'company')}")

    def list_clients(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/clients") or []

    def create_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Only email and names are sent: a ``company`` key would make PwnDoc upsert a company
        nobody journals, so the client is linked to the audit through ``PUT /general``."""
        body = {k: payload[k] for k in CLIENT_KEYS if k in payload}
        data = self._object(self._request("POST", "/api/clients", json=body), "client")
        self._new_id(data, "client")
        return data

    def delete_client(self, client_id: str) -> None:
        self._request("DELETE", f"/api/clients/{safe_id(client_id, 'client')}")

    def create_audit(self, name: str, language: str, audit_type: str) -> str:
        body = {"name": name, "language": language, "auditType": audit_type}
        data = self._object(self._request("POST", "/api/audits", json=body), "audit")
        return self._new_id(data.get("audit"), "audit")

    def get_audit_general(self, audit_id: str) -> dict[str, Any]:
        path = f"/api/audits/{safe_id(audit_id, 'audit')}/general"
        return self._object(self._request("GET", path), "audit general")

    def update_audit_general(self, audit_id: str, payload: dict[str, Any]) -> None:
        self._request("PUT", f"/api/audits/{safe_id(audit_id, 'audit')}/general", json=payload)

    def delete_audit(self, audit_id: str) -> None:
        self._request("DELETE", f"/api/audits/{safe_id(audit_id, 'audit')}")

    def create_finding(self, audit_id: str, payload: dict[str, Any]) -> None:
        """PwnDoc answers a plain message, never the new finding id (re-read the audit)."""
        body = {k: v for k, v in payload.items() if k != "_id"}
        self._request("POST", f"/api/audits/{safe_id(audit_id, 'audit')}/findings", json=body)

    def delete_finding(self, audit_id: str, finding_id: str) -> None:
        path = f"/api/audits/{safe_id(audit_id, 'audit')}/findings/{safe_id(finding_id, 'finding')}"
        self._request("DELETE", path)

    def create_image(self, value: str, name: str, audit_id: str) -> str:
        """``value`` is a ``data:`` URL. PwnDoc deduplicates by value and may return the id of
        an existing row that belongs to another audit (check with ``get_image``)."""
        body = {"value": value, "name": name, "auditId": safe_id(audit_id, "audit")}
        return self._new_id(self._request("POST", "/api/images/", json=body), "image")

    def get_image(self, image_id: str) -> dict[str, Any]:
        path = f"/api/images/{safe_id(image_id, 'image')}"
        return self._object(self._request("GET", path), "image")

    def delete_image(self, image_id: str) -> None:
        self._request("DELETE", f"/api/images/{safe_id(image_id, 'image')}")
