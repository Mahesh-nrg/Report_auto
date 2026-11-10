"""A small stateful fake PwnDoc for the writer, lookups and undo tests (httpx.MockTransport).

Imported by the sibling test files (``tests/docx_import`` has no ``__init__.py``, so pytest puts
this directory on ``sys.path``). It holds no test functions of its own except a self-check.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx

from pwndoc_ai.docx_import.model import (
    AuditHeader,
    HeaderFields,
    ImportedFinding,
    ImportedImage,
    ImportPlan,
    PocBlock,
)
from pwndoc_ai.docx_import.records import AuditTypeOption, Choice, PwnDocOptions
from pwndoc_ai.pwndoc_client import PwnDocClient

IMPORT_ID = "0123456789ab4def8123456789abcdef"
TEMPLATE_ID = "7" * 24
SHA = "0" * 64


def png(width: int = 4, height: int = 3, seed: int = 0) -> bytes:
    """A small valid PNG; ``seed`` changes the bytes (and so the PwnDoc dedupe key)."""
    raw = b"".join(b"\x00" + bytes([seed % 256]) * (width * 3) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw) + b"\x00" * 300)
        + chunk(b"IEND", b"")
    )


def ok(datas: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": "success", "datas": datas})


def err(message: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"status": "error", "datas": message})


@dataclass
class Rule:
    method: str
    pattern: str
    status: int
    nth: int = 1  # fail on the nth matching call
    seen: int = 0


@dataclass
class FakePwnDoc:
    companies: list[dict] = field(default_factory=list)
    clients: list[dict] = field(default_factory=list)
    audits: dict[str, dict] = field(default_factory=dict)
    images: dict[str, dict] = field(default_factory=dict)
    custom_fields: list[dict] = field(default_factory=list)
    languages: list[dict] = field(default_factory=lambda: [{"language": "English", "locale": "en"}])
    audit_types: list[dict] = field(
        default_factory=lambda: [
            {
                "name": "Web",
                "stage": "default",
                "templates": [{"template": TEMPLATE_ID, "locale": "en"}],
            }
        ]
    )
    templates: list[dict] = field(
        default_factory=lambda: [{"_id": TEMPLATE_ID, "name": "Final_1", "ext": "docx"}]
    )
    admin: bool = False
    vulnerabilities: list[dict] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    bodies: list[tuple[str, str, Any]] = field(default_factory=list)
    counter: int = 0

    # -- helpers ------------------------------------------------------------------------------
    def new_id(self) -> str:
        self.counter += 1
        return f"{self.counter:024x}"

    def fail(self, method: str, pattern: str, status: int, nth: int = 1) -> None:
        self.rules.append(Rule(method, pattern, status, nth))

    def open_client(self):
        return PwnDocClient("https://pwndoc.test", transport=httpx.MockTransport(self))

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def add_audit(
        self, name: str, company_id: str | None = None, created: str = "2026-01-01"
    ) -> str:
        audit_id = self.new_id()
        company = None if company_id is None else {"_id": company_id, "name": "x"}
        self.audits[audit_id] = {
            "_id": audit_id, "name": name, "company": company, "createdAt": created,
            "language": "en", "auditType": "Web", "findings": [], "general": {"template": TEMPLATE_ID},
        }  # fmt: skip
        return audit_id

    def _injected(self, method: str, path: str) -> httpx.Response | None:
        for rule in self.rules:
            if rule.method == method and re.fullmatch(rule.pattern, path):
                rule.seen += 1
                if rule.seen == rule.nth:
                    if rule.status == 500:
                        return httpx.Response(500, text="Something went wrong.")
                    return err("injected", rule.status)
        return None

    # -- transport ----------------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else None
        self.bodies.append((method, path, body))
        injected = self._injected(method, path)
        if injected is not None:
            return injected
        for route_method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if match and route_method == method:
                return handler(body, *match.groups())
        return err("not found", 404)

    def _routes(self):
        hexid = "([0-9a-f]{24})"
        return [
            ("GET", "/api/companies", lambda b: ok(self.companies)),
            ("POST", "/api/companies", self._create_company),
            ("DELETE", f"/api/companies/{hexid}", self._delete_from("companies")),
            ("GET", "/api/clients", lambda b: ok(self.clients)),
            ("POST", "/api/clients", self._create_client),
            ("DELETE", f"/api/clients/{hexid}", self._delete_from("clients")),
            ("GET", "/api/audits", lambda b: ok([self._summary(a) for a in self.audits.values()])),
            ("POST", "/api/audits", self._create_audit),
            ("GET", f"/api/audits/{hexid}", self._get_audit),
            ("DELETE", f"/api/audits/{hexid}", self._delete_audit),
            ("GET", f"/api/audits/{hexid}/general", self._get_general),
            ("PUT", f"/api/audits/{hexid}/general", self._put_general),
            ("POST", f"/api/audits/{hexid}/findings", self._create_finding),
            ("POST", "/api/images/", self._create_image),
            ("GET", f"/api/images/{hexid}", self._get_image),
            ("DELETE", f"/api/images/{hexid}", self._delete_image),
            ("GET", "/api/data/custom-fields", lambda b: ok(self.custom_fields)),
            ("GET", "/api/data/languages", lambda b: ok(self.languages)),
            ("GET", "/api/data/audit-types", lambda b: ok(self.audit_types)),
            ("GET", "/api/templates", lambda b: ok(self.templates)),
            ("GET", "/api/users/me", lambda b: ok(self._me())),
            ("GET", "/api/vulnerabilities", lambda b: ok(self.vulnerabilities)),
            ("GET", "/api/vulnerabilities/([a-zA-Z-]{2,10})", lambda b, loc: ok([])),
        ]

    def _me(self) -> dict:
        return {
            "username": "alice" if self.admin else "bob",
            "role": "admin" if self.admin else "user",
        }

    def _summary(self, audit: dict) -> dict:
        keys = ("_id", "name", "company", "createdAt", "language", "auditType")
        return {k: audit[k] for k in keys}

    def _create_company(self, body):
        if any(c["name"] == body["name"] for c in self.companies):
            return err("Company name already exists", 422)
        company = {"_id": self.new_id(), "name": body["name"]}
        self.companies.append(company)
        return ok(company, 201)

    def _create_client(self, body):
        contact = {"_id": self.new_id(), **body}
        self.clients.append(contact)
        return ok(contact, 201)

    def _delete_from(self, name: str):
        def handler(body, object_id):
            rows = getattr(self, name)
            if not any(r["_id"] == object_id for r in rows):
                return err("not found", 404)
            setattr(self, name, [r for r in rows if r["_id"] != object_id])
            return ok("deleted")

        return handler

    def _create_audit(self, body):
        if body["auditType"] not in {t["name"] for t in self.audit_types}:
            return err("AuditType not found", 404)
        audit_id = self.add_audit(body["name"], None, "2026-10-23")
        self.audits[audit_id]["language"] = body["language"]
        return ok({"message": "Audit created successfully", "audit": {"_id": audit_id}}, 201)

    def _get_audit(self, body, audit_id):
        audit = self.audits.get(audit_id)
        return ok(audit) if audit else err("Audit not found", 404)

    def _delete_audit(self, body, audit_id):
        if self.audits.pop(audit_id, None) is None:
            return err("Audit not found", 404)
        return ok({"_id": audit_id})

    def _get_general(self, body, audit_id):
        audit = self.audits.get(audit_id)
        return ok(audit["general"]) if audit else err("Audit not found", 404)

    def _put_general(self, body, audit_id):
        audit = self.audits.get(audit_id)
        if audit is None:
            return httpx.Response(500, text="Something went wrong.")
        audit["general"] = {**audit["general"], **body}
        if "company" in body:
            audit["company"] = {"_id": body["company"]["_id"], "name": "x"}
        return ok("Audit General updated successfully")

    def _create_finding(self, body, audit_id):
        audit = self.audits.get(audit_id)
        if audit is None:
            return httpx.Response(500, text="Something went wrong.")
        number = len(audit["findings"]) + 1
        audit["findings"].append({"_id": self.new_id(), "identifier": number, **body})
        return ok("Audit Finding created successfully")

    def _create_image(self, body):
        for image_id, row in self.images.items():
            if row["value"] == body["value"]:
                return ok({"_id": image_id}, 201)  # PwnDoc dedupes by value
        image_id = self.new_id()
        self.images[image_id] = {
            "auditId": body["auditId"],
            "value": body["value"],
            "name": body["name"],
        }
        return ok({"_id": image_id}, 201)

    def _get_image(self, body, image_id):
        row = self.images.get(image_id)
        return ok({"_id": image_id, **row}) if row else err("Image not found", 404)

    def _delete_image(self, body, image_id):
        if not self.admin:
            return err("Insufficient privileges", 403)
        if self.images.pop(image_id, None) is None:
            return err("Image not found", 404)
        return ok("deleted")


@contextlib.contextmanager
def opened(fake: FakePwnDoc):
    with fake.open_client() as client:
        yield client


def image(
    key: str, finding_key: str, data: bytes, order: int, caption: str = "Login", **kw
) -> ImportedImage:
    return ImportedImage(
        key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=kw.pop("media_type", "image/png"),
        size=len(data),
        width=4,
        height=3,
        caption=caption,
        finding_key=finding_key,
        source_finding_key=finding_key,
        order=order,
        **kw,
    )


def finding(key: str, title: str, **kw) -> ImportedFinding:
    kw.setdefault("poc", (PocBlock(kind="html", html="<p>Step 1</p>"),))
    return ImportedFinding(key=key, title=title, **kw)


def make_plan(findings, images=(), **header) -> ImportPlan:
    values = {
        "company": "Brandarmy", "audit_name": "brandarmy", "language": "en", "audit_type": "Web",
        "template_id": TEMPLATE_ID, "date_start": "2026-03-05", "date_end": "",
        "scope": ("https://brandarmy.com", "https://wink.ing/"),
    }  # fmt: skip
    values.update(header)
    return ImportPlan(
        import_id=IMPORT_ID,
        revision=1,
        file_name="brandarmy.docx",
        file_sha256=SHA,
        header=AuditHeader(**values),
        source_header=HeaderFields(),
        findings=tuple(findings),
        images=tuple(images),
        options=PwnDocOptions(
            languages=(Choice("en", "English"),),
            audit_types=(AuditTypeOption("Web", ("en",)),),
            templates=(Choice(TEMPLATE_ID, "Final_1"),),
        ),
    )


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def test_fake_dedupes_images_by_value():
    fake = FakePwnDoc()
    first = fake._create_image({"value": "v", "auditId": "a", "name": "n"}).json()["datas"]["_id"]
    again = fake._create_image({"value": "v", "auditId": "b", "name": "n"}).json()["datas"]["_id"]
    assert first == again and fake.images[first]["auditId"] == "a"
