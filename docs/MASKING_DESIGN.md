# Realistic-Dummy Masking — Design Contract

Status: design agreed, builders implement against it. This replaces the old
`[[CLIENT_1]]`/`[[IP_1]]` placeholder scheme. Instead of opaque tokens, the model now sees
**believable stand-in values** (a fictional company name, fictional domains/hosts, fictional but
structurally faithful IPs/e-mails) so it can still read the pattern of a finding; the model's
answer is then converted **back to the real client values** before anything is stored or sent to
PwnDoc.

The user's rule, verbatim: *mask the client names and IPs — send believable dummy client names,
domains and IPs to the AI so it still understands the pattern, convert back to the originals in
the response; only the client-identifying parts change. Endpoints (URL paths, API routes, query
parameter names) and the payloads used in the PoC go to the LLM as they are.*

---

## 0. One-paragraph summary of the swap

Change the **host** of a URL, never its scheme/port/path/query/fragment. Change a **domain** but
keep its sub-domain labels (`app`, `api`, `admin`, `vpn`). Change a client **name** into a
fictional company of the same shape (suffix, word count, casing preserved). Change an **IP** into
one that keeps its class and `/24` topology. Change an **e-mail** domain (and the local part only
when it is not a generic role). Everything else — paths, query names, SQLi/XSS/RCE payloads,
request bodies, headers, cookies, tokens, curl/Burp text — is byte-for-byte untouched **unless it
literally contains a client identifier**, in which case only that identifier is swapped.

---

## 1. Module map (one owner per file)

`masking.py` stays the public **facade** and keeps every exported name and signature below. The
mechanism moves into focused modules, all under the M-CORE owner.

| File | Owner | Responsibility |
|------|-------|----------------|
| `masking_model.py` | M-CORE | Shared frozen dataclasses, enums, limits, pools, errors. **Done.** No mapping/secret state. |
| `masking_names.py` | M-CORE | Company-name pools + deterministic, shape-preserving dummy company generator (suffix/case/word-count, per-instance salt, distinct→distinct, no real brands). |
| `masking_net.py` | M-CORE | Registrable-domain derivation (incl. IDN/punycode, public-suffix list), host/domain map (sub-domains kept), IPv4 keyed `/24` map per range kind, IPv6 `2001:db8::/32`, URL host-only rewrite, e-mail local-part + domain rule. |
| `masking_text.py` | M-CORE | Regex assembly, HTML/tag-aware segmenting, the forward pass that routes each match to the right builder, and `unmask` with its perturbation tolerance. |
| `masking.py` | M-CORE | `Masker`, `NullMasker`, `build_masker`, constants re-export, the public methods — thin orchestration over the three builders + a shared `_Mappings` registry (lock-guarded). |

### 1.1 Public API (unchanged signatures, callers keep working)

```python
MIN_TERM_LEN: int; MAX_TERM_LEN: int; MAX_TERMS: int; MAX_DEPTH: int   # re-exported from model

class Masker:
    def __init__(self, terms: Iterable[tuple[str, str]] = ()) -> None: ...
    @classmethod
    def from_audit(cls, audit: object, extra_terms: Iterable[str] = ()) -> "Masker": ...
    enabled: bool                                    # True
    def mask(self, text) -> str                      # HTML-aware (href/alt/title in tags only)
    def mask_plain(self, text) -> str                # never treats <...> as markup
    def mask_json(self, value) -> Any                # rich recursion, HTML-aware leaves
    def mask_json_plain(self, value) -> Any          # rich recursion, plain leaves
    def unmask(self, value) -> Any                   # str/dict/list/tuple; raises ValueError past MAX_DEPTH
    def leftovers(self, value) -> list[str]          # see 1.2
    def sensitive_kinds(self, text) -> list[str]     # CLIENT/AUDIT/TERM/DOMAIN/IP/EMAIL/URL
    def __repr__(self) -> str                        # counts only, never the mapping

class NullMasker: ...                                # identity; enabled False; leftovers/sensitive_kinds []
def build_masker(audit, enabled) -> Masker | NullMasker
```

### 1.2 New API (named in the contract; call sites reach them via `getattr` so test fakes survive)

```python
def reserve(self, text_or_json) -> ReserveResult     # register strings already in a document
def prepare(self, corpus) -> ReserveResult           # reserve() over an iterable/JSON corpus
def redact(self, text) -> str                        # client values REMOVED (no dummies) — web queries
def originals_in(self, masked_text) -> list[str]     # the real values behind dummies present in text
def dummies_in(self, value) -> list[str]             # issued dummies/aliases still present
def dummy_leaks(self, value) -> list[DummyLeak]      # dummies_in + fake-pool addresses, typed
def assert_no_dummies(self, value) -> None           # raises DummyLeakError if dummy_leaks non-empty
```

`leftovers(value)` keeps its signature and its diff-friendly behaviour. It returns **both** any
legacy `[[KIND_n]]` strays (for old in-flight data) **and** the result of `dummies_in(value)`, as a
deduplicated `list[str]`. So the existing `leftovers(answer) - leftovers(originals)` idiom in
`refiner`, `maskio` and `maskflow` keeps meaning "stand-ins the model emitted that were not in the
input", and `maskflow._has_placeholder_text` keeps failing closed.

---

## 2. Exact mapping rules, with worked examples

Alternation order in the master regex (first match at an offset wins) changes to put hosts and
domains **before** the term rule, so a client word inside a host does not get half-masked:

```
lit(legacy) | url | email | ipv4 | ipv6 | domain/host | term
```

### 2.1 Company / client names → fictional company (`masking_names.py`)

* Curated embedded pools of invented company roots (e.g. `Lumenfold`, `Northgate`, `Verdana Works`,
  `Kestrel Dynamics`) — no real well-known brands; multi-word and single-word roots in separate
  pools so word count can be matched.
* Preserve the **legal suffix** exactly as written: `Inc`, `Inc.`, `Ltd`, `LLC`, `GmbH`, `Corp`,
  `Corporation`, `AG`, `S.A.`, `Pty Ltd`… The real suffix token is detected and re-emitted
  verbatim on the dummy.
* Preserve **word count** of the non-suffix part (one-word client → one-word dummy).
* Preserve **capitalisation style** of the whole surface: `ALL UPPER`, `lower`, `Title Case`,
  `Mixed` → apply the same casing transform to the dummy.
* Distinct real clients → distinct dummies (generator skips a candidate already taken).
* Deterministic within one `Masker` (same real value → same dummy across chunks/calls), randomised
  across instances via a per-instance salt (`SALT_BYTES` of `os.urandom`, never persisted/logged).
  Candidate index = keyed hash(salt, normalised-original) walked until a collision-free pick.

Worked:

| Real | Dummy (one possible salt) |
|------|---------------------------|
| `Brandarmy Inc` | `Lumenfold Inc` |
| `BRANDARMY` | `LUMENFOLD` |
| `brandarmy` | `lumenfold` |
| `Acme Security Group` | `Northgate Kestrel Group` |

A dummy company name must not be a `GENERIC_WORDS` member and must not collide with a reserved
corpus token (§4).

### 2.2 Domains / hostnames (`masking_net.py`)

* Derive the **registrable domain** (eTLD+1). Use an embedded public-suffix list covering the
  common multi-label suffixes (`co.uk`, `com.au`, `co.jp`, `org.uk`, …) plus the single-label
  default; fall back to "last two labels" only when the suffix list does not match.
* Swap the **registrable domain** for a fictional one under a reserved zone (`<fake>.example`,
  `<fake>.test`, or `example.<tld>`) so it can never resolve to a real third party.
* **Keep every sub-domain label** (`app`, `api`, `admin`, `vpn`, `www`, `mail`): they carry the
  pattern and are not client-identifying.
* All hosts sharing a real registrable domain map to the **same** fake registrable domain.
* Case-insensitive. IDN/punycode: decode `xn--` and NFKC-fold before lookup; emit an ASCII
  fictional host. Ports preserved (handled by the URL/host matcher, not stored in the host).
* When the registrable label equals or contains the client company term (e.g. `brandarmy.com` for
  client *Brandarmy*), derive the fake domain label from the **fake company slug**
  (`lumenfold.example`) so name and domain stay coherent.
* **Public reference hosts are NOT rewritten.** Reuse `docx_engine/facts.TRUSTED_HOSTS` plus the
  "client-related?" test (`_url_is_client`: a known term word, an embedded IP, or a host/domain
  registered via `reserve`/the audit). A URL whose host is neither trusted nor client-related is
  left exactly as written, so `https://owasp.org/...`, `cwe.mitre.org`, `nvd.nist.gov` reach the
  model intact.

Worked:

| Real | Dummy |
|------|-------|
| `app.brandarmy.com` | `app.lumenfold.example` |
| `admin.brandarmy.com` | `admin.lumenfold.example` (same registrable → same fake) |
| `vpn.wink.ing` | `vpn.northgate.example` (different real domain → different fake) |
| `shop.example.co.uk` | `shop.verdana.example` (co.uk recognised as suffix) |
| `bücher.brandarmy.com` | `bucher.lumenfold.example` (IDN folded) |
| `owasp.org` | `owasp.org` (trusted, untouched) |

### 2.3 URLs — host only (`masking_net.py` + `masking_text.py`)

The URL token is no longer swallowed whole. The matcher finds the URL span (same `&amp;`
entity-boundary rule and `_split_trailing` tail logic as today, capped at `MAX_TRAILING_STRIP`
chars — see §7), splits off the **authority** (`userinfo@host:port`), rewrites only the **host**
through the domain map, and leaves scheme, userinfo, port, path, query, fragment **byte-for-byte**.
Terms occurring inside the path/query text are still replaced for consistency (§2.8), but a path
or payload containing no client identifier is untouched.

Worked:

```
https://app.brandarmy.com:8443/login?next=/admin&id=1' OR '1'='1
→ https://app.lumenfold.example:8443/login?next=/admin&id=1' OR '1'='1
```

Path (`/login`), query names (`next`, `id`), and the SQLi payload (`1' OR '1'='1`) all pass through
unchanged. Same for `mailto:` hrefs (address via the e-mail rule) and the `href`/`alt`/`title`
attribute handling (the HTTP-href special case in `_mask_attr` becomes an ordinary host swap).

### 2.4 IPv4 (`masking_net.py`)

Keyed **injective** map of the `/24` prefix; the **host octet** and the **address class** are kept.

* Private ranges map to a different prefix **in the same private kind** (10/8→10/8, 172.16/12→
  172.16/12, 192.168/16→192.168/16), so a private address still looks private.
* Public addresses map into fake pools that are non-routable but realistic:
  `198.18.0.0/15` (benchmarking), `203.0.113.0/24` (documentation), `100.64.0.0/10` (CGNAT).
* Two hosts of the same real `/24` land in the same fake `/24` (topology preserved). The host
  octet is carried over unchanged.
* **CIDR masks and ranges are preserved** by matching only the address and leaving the suffix
  outside: `10.0.0.0/24` → `10.<k>.<k>.0/24`; `10.0.0.1-10.0.0.9` masks both ends into the same
  fake `/24`.
* **Never masked** (left as written): loopback `127/8`, `0.0.0.0`, `255.255.255.255`, link-local
  `169.254/16`, multicast `224/4`, and version-like strings (`v1.2.3.4`, `1.2.3.4.5`,
  `10.0.0.1abc`, `1.2.3.4-beta`). Octets keep the strict no-leading-zero rule. An address glued
  after a host label (`app.192.168.1.1`) IS masked: only a word character or a `<digit>.` before
  the first octet blocks the match, so the address cannot leak through a malformed host.
* Real-sample demo: `tests/test_masking_samples_demo.py` (skips without `/tmp/refine_test` or
  `/tmp/layout`) runs Finish, the DOCX chunker and the importer finding call on the Brandarmy
  and demo reports; run it with `-s` to print what the model receives and what is stored.

Worked: `192.168.1.1/24` → `192.168.<k>.1/24`; `10.0.0.1` and `10.0.0.9` → `10.<k>.<k>.1` and
`10.<k>.<k>.9` (same fake /24).

### 2.5 IPv6 (`masking_net.py`)

Map into `2001:db8::/32`, keeping the low bits (host portion) so `2001:db8:...` structure is
plausible and distinct hosts stay distinct. Validate with `ipaddress` before mapping (so `::1`,
`std::string`, `12:30:45`, `a:b:c` are not masked); `::1` loopback stays unmasked.

### 2.6 E-mail (`masking_net.py`)

* Local part kept when it is a generic role (`ROLE_LOCAL_PARTS`: admin, info, support, security,
  noreply, it, hr, sales…); otherwise replaced by a believable fictional local part.
* The **domain** always goes through the domain map.
* `admin@brandarmy.com` → `admin@lumenfold.example`; `john.doe@brandarmy.com` →
  `<fake-local>@lumenfold.example`.

### 2.7 Host headers, cookies, curl, Burp/raw HTTP

These are plain text to the masker. Only the client-identifying substrings change:

```
GET /api/v2/users?id=7 HTTP/1.1
Host: app.brandarmy.com            → Host: app.lumenfold.example
Cookie: session=eyJ...; csrf=abc   → unchanged (token is not a client identifier)
Authorization: Bearer eyJhbGci...  → unchanged
curl 'https://api.brandarmy.com/login' -d "user=admin&pass=p@ss"
   → curl 'https://api.lumenfold.example/login' -d "user=admin&pass=p@ss"
```

The path, query, method, cookie/token values and body stay byte-for-byte.

### 2.8 Known terms (names/audit/extra)

Matched case-insensitively, word-boundary, longest-first (existing behaviour), and also replaced
inside URL paths/payload text wherever they literally occur (consistency). A payload that contains
**none** of the client's identifiers is left byte-for-byte — including when it contains `&amp;`,
`<`, or `[[ ... ]]` shell text. New property to test: "payload without client identifiers is
unchanged".

---

## 3. Dummy pools & collision-free generation

* Pools live embedded in `masking_names.py` (companies) and `masking_net.py` (fake-local-part
  roots). No new dependency, no `faker`.
* Generation for a real value: `index = keyed_walk(salt, norm(original))`; walk the pool until a
  candidate that is (a) not already assigned to another original, (b) not a substring of any other
  issued dummy and no other issued dummy is a substring of it, (c) not present in the reserved
  corpus, (d) not equal to any real value. Bounded by `MAX_DUMMY_ATTEMPTS`; on exhaustion, append a
  deterministic disambiguating suffix drawn from the salt (keeps shape, stays collision-free).
* **Aliases.** For a company, also register the suffix-less form and, for a domain, the bare
  registrable domain, as `Mapping(alias=True)` reverse entries, so a model that drops the suffix or
  sub-domain still unmasks and still trips the dummy-leak scan.

### 3.1 `reserve` / `prepare`

`reserve(text_or_json)` walks strings (bounded by `MAX_RESERVE_CHARS`), tokenises into words,
hosts, domains, IPs and e-mail local parts, and adds them to the per-instance reserved set so no
future dummy equals corpus text. `prepare(corpus)` is `reserve` over an iterable/JSON. Callers
reserve the **whole document/audit** before the first mask. Returns `ReserveResult` (counts only).

### 3.2 Late collision (new text literally contains an already-issued dummy)

`CollisionPolicy`:

* **Nothing sent yet for that dummy** → `REASSIGN`: pick a new collision-free dummy for that
  original and update the registry (safe; the old dummy was never externalised).
* **Already sent** → `REFUSE`: raise `DummyCollisionError`. The call site treats it like any other
  masking failure (fail closed). This guarantees unmask can never corrupt genuine text by turning a
  coincidental literal into the wrong original.

---

## 4. Un-masking algorithm & perturbation tolerance

Build one precompiled alternation of **all issued dummies and aliases**, longest-first, compiled
case-insensitively, with word-boundary guards. Single pass over the model's answer; restored text
is never re-scanned (no double restore).

Restore the **original surface casing** from the style of the dummy occurrence (UPPER/lower/Title),
falling back to the canonical stored original. Must survive these model perturbations (each has a
test):

* possessive `'s` (`Lumenfold's` → `Brandarmy's`)
* markdown emphasis/backticks (`**Lumenfold**`, `` `lumenfold.example` ``)
* HTML escaping (`html.escape` of the masked text round-trips)
* surrounding punctuation / being inside a sentence
* being inside a URL, an e-mail, or a fenced code block
* URL-encoded host (`lumenfold%2eexample` → percent-decode on the leak-check side)
* case changes and Title-casing by the model

Not required: a dummy split across a hyphen/line-wrap. `unmask` still accepts legacy `[[KIND_n]]`
for cheap old-data compatibility but no longer emits it.

---

## 5. Leak checks & per-call-site failure behaviour

Two independent guards, both mandatory (no longer `getattr`-optional at the guard itself):

1. **Outgoing (pre-send):** no ORIGINAL client value (name, host, registrable domain, IP, e-mail,
   decoded host forms) appears in the text about to be sent. This is the `sensitive_kinds` /
   `redact`-based check. `OriginalLeakError` / a non-empty `sensitive_kinds` ⇒ do not send.
2. **Incoming (post-unmask):** `assert_no_dummies(value)` scans with the dummy/alias alternation and
   `in_fake_pool` for stray `198.18/15`, `203.0.113/24`, `100.64/10`, `2001:db8::/32` addresses
   (which can never be legitimate in a real answer). Any hit ⇒ `DummyLeakError` ⇒ reject the answer,
   exactly as a stray placeholder is rejected today.

Per call site, on failure:

| Call site | Guard point | On failure |
|-----------|-------------|-----------|
| refiner.refine | after `_unmask`, with the stray check | raise `ValueError` → job fails closed |
| summary | after `_unmask` of summary + each roadmap item | raise `SummaryError` → HTTP 502 |
| docx chunk_run `_judge` | after `adapter.unmask` | reject chunk ("a protected name or address was removed" / "unrestored stand-in") |
| importer `call_masked` | after `unmask` | `FallbackRejected` |
| library | `sensitive_kinds` on raw save text | refuse to save, fail closed if masker missing |
| web query | `redact` before egress | drop query if it still carries a real value |

---

## 6. Per-call-site change list (owners per FILE OWNERSHIP boundaries)

M-SITES owns all of these unless noted.

* `refiner.py` — drop `PLACEHOLDER_TOKEN`; `_research_query` → `masker.redact` (fallback to old
  behaviour for fake maskers); `reserve` the draft/custom values at the top of `_build_request`;
  pass a **redacted** excerpt to the context provider (stable research-cache key, no fake names
  embedded); mask research/library text through the collision-safe path; extend
  `_reject_stray_placeholders` to also reject remaining dummies / fake-pool addresses.
* `prompts.py`, `docx_engine/prompts.py`, `docx_import/fallback_prompts.py` — new stand-in wording
  (§7); remove the `[[CLIENT_1]] withheld data` sentences. `HEADER_ROLE` stays unmasked wording.
* `summary.py` — stand-in sentence in SYSTEM_PROMPT; dummy-leak check on `executive_summary` and
  each roadmap item; reserve audit/findings before masking.
* `qa.py` — keep the legacy `[[...]]` lint for old data (comment it as legacy); no masker available.
* `library.py` — `sensitive_kinds` must also report client DOMAIN; no dummy dependence.
* `docx_engine/maskio.py` — rewrite: `dropped()` via `masker.originals_in` (case-insensitive
  compare); `stray()` via dummies-remaining; `strip_placeholders` → `masker.redact`; add
  `prepare/reserve` passthrough; drop the `PLACEHOLDER`/`ANY_PLACEHOLDER` regexes (optional legacy
  check only).
* `docx_engine/refine.py` — `adapter.prepare(whole document text incl. titles)` after
  `load_document`, before the first chunk; keep fail-closed `MASK_FAILED`.
* `docx_engine/research.py` — `adapter.redact` for the query; collision-safe masking of fetched
  research.
* `docx_engine/chunk_run.py` — stray/dropped only through the adapter.
* `docx_import/maskflow.py` — **mask values before JSON serialisation** (fixes the verified
  newline/tab leak); route host-shaped terms to the domain map; `reserve` the report corpus;
  `_has_placeholder_text` also checks remaining dummies; keep `call_header` unmasked and
  `has_client_name` fail-closed.
* `docx_import/fallback.py`, `fallback_prompts.py` — `finding_messages` masks items not the
  serialised string; reserve before the first batch.
* `jobs.py`, `cli.py`, `server_features.py` — no signature change (auto-prepare from the audit in
  `from_audit`); optionally log a value-free warning when masking is on but the audit has no
  client/company term.
* UI/docs (M-HARNESS): `static/ai-settings.js`, `static/ai-docx-outline.js`, `static/ai-import.js`,
  `README.md`, `docs/ARCHITECTURE.md`, `docs/IMPORTER_DESIGN.md` — describe realistic stand-ins and
  state plainly that endpoints/paths/query names/payloads/headers/cookies/tokens that are not
  client identifiers are sent as-is, and that a secret in a payload is not masked unless it equals
  a client term.

---

## 7. Prompt wording (replaces the `[[CLIENT_1]]` sentences)

> Names, domains, host names, e-mail addresses and IP addresses in this text are real values for
> this engagement. Treat them as real and copy each one **exactly as written**, character for
> character. Do not comment on them, do not call them examples or placeholders, and do not invent
> or alter any name, host or address. Copy every URL, path, query parameter and payload exactly.

(The stand-in nature is never revealed to the model; from its side the values simply are the data.)

---

## 8. Performance

Everything stays linear-time on 1 MB hostile input: precompiled regex, bounded quantifiers,
lookbehind guards, `_split_trailing` capped at `MAX_TRAILING_STRIP` (fixes the old quadratic
`url[:end].count(...)` path — add `"http://x/"+")"*MB` and `"]"*MB` to `_HOSTILE`). The dummy
alternation for unmask is a single precompiled regex, longest-first. Thread-safe via one
`threading.Lock` over the registry, as today.

---

## 9. Test plan

**Keep (strings change only):** term matching (case/boundary/whitespace/entities), e-mail/URL
tokenising, HTML tag safety (href/alt/title only; class/style/data-* left alone — documented
limit), JSON key/type/non-mutation, MAX_DEPTH boundaries, NullMasker identity, `build_masker`
switch, repr-no-leak, thread safety, the 400/60-seed round-trip and leak properties.

**Adapt (new mechanism):** dummy typing (company-shaped / reserved-zone domain / fake-pool IP);
collision tests (reserved corpus, later-literal reassign/refuse, substring-free); unmask robustness
(possessive/markdown/HTML-escape/URL-encoded host/inside URL-email-code/casing); "looks like a
dummy but never issued stays as is"; `sensitive_kinds` for new hosts/IPs; a seed variant where two
Maskers get different salts.

**Drop:** `[[KIND_n]]` format asserts, LIT escaping/nesting, broken-placeholder leftover family,
markdown-escaped-underscore unmask.

**Add:** host-only URL rewrite keeps path/query/payload byte-for-byte; payload-without-identifier
untouched; kept sub-domain labels; same real /24 → same fake /24, CIDR/range preserved; skips
(127/8, 0.0.0.0, 255.255.255.255, link-local, multicast, version strings); IDN/punycode + NFKC;
research-query redaction (no dummies, no real values); summary/refiner reject remaining dummies;
importer newline/tab-adjacent masking; DOCX chunk consistency across chunks and split retry;
adversarial-echo model (echo, change case, add possessive, wrap in markdown, mangle one dummy);
1 MB hostile input linear time (new domain/host/IPv6/bracket shapes).

**Fixture migration:** every `[[CLIENT_1]]`/`[[IP_1]]` literal assert in `test_refiner*.py`,
`test_prompts.py`, `test_gateway_wiring.py` (SeenLlm), `test_features_api.py` (summary),
`test_docx_refine_safety.py`, `docx_import/test_maskflow.py`, `docx_import/test_fallback.py`
becomes an assert against the issued dummy (obtained from the live Masker in the test) or the
stand-in prompt wording. `test_qa.py` stays unchanged (legacy lint).

---

## 10. BUILD_SCHEMA — decisions every builder must know

1. Facade `masking.py` keeps all public names/signatures; mechanism split into
   `masking_model.py` (done), `masking_names.py`, `masking_net.py`, `masking_text.py`.
2. Alternation order: `lit | url | email | ipv4 | ipv6 | domain | term` (hosts before terms).
3. URL = host-only swap; path/query/fragment/port/payload byte-for-byte. Trusted + non-client
   hosts untouched (reuse `facts.TRUSTED_HOSTS` + `_url_is_client`).
4. Domain = registrable-domain swap under `.example`/`.test`; sub-domains kept; eTLD+1 via embedded
   suffix list; IDN/punycode NFKC-folded; same registrable → same fake; domain label derived from
   fake company slug when it matches the client name.
5. Names = shape-preserving fictional company (suffix/case/word-count), curated pools, no real
   brands, deterministic-per-instance via salt, distinct→distinct, suffix-less alias registered.
6. IPv4 = keyed /24 map keeping host octet + class; private→same-kind-private,
   public→198.18/15·203.0.113/24·100.64/10; CIDR/range preserved; skip loopback/0.0.0.0/broadcast/
   link-local/multicast/version strings. IPv6 = `2001:db8::/32` low-bits kept.
7. E-mail = role local part kept else fictional; domain via domain map.
8. Collision-free dummies: never in reserved corpus, never substring of another dummy, never equal
   a real value. `reserve`/`prepare` register the whole document/audit up front. Late literal:
   reassign if unsent, else `DummyCollisionError`.
9. Unmask: single-pass dummy+alias alternation, longest-first, case-insensitive, boundary-aware;
   restores original casing by style; tolerates possessive/markdown/HTML-escape/URL-encoded-host/
   inside-URL-email-code. Accepts legacy `[[KIND_n]]`, never emits it.
10. Leaks: pre-send (no original value out) + post-unmask `assert_no_dummies` (dummies + fake-pool
    addresses) → `DummyLeakError`, mandatory at every call site. `leftovers` returns legacy strays +
    `dummies_in` so the `leftovers(answer)-leftovers(originals)` idiom keeps working. A
    `.example` host counts as a leak ("fake-zone") only when its registrable label resembles an
    issued stand-in domain (equal, contained, or a shared 5-letter prefix), so a mangled stand-in
    is caught while model-written RFC 2606 hosts such as `attacker.example` are accepted.
11. New API reached via `getattr` at call sites (`reserve`/`prepare`/`redact`/`originals_in`/
    `dummies_in`/`dummy_leaks`/`assert_no_dummies`) so existing test fakes keep passing.
12. `masking_model.py` is the shared source of truth for kinds, limits, pools, records, errors;
    builders import from it rather than redefining.
13. Prompts: new stand-in wording, no "placeholder/withheld" language; `HEADER_ROLE` stays
    unmasked.
14. Known unmasked-by-design paths unchanged and documented: importer header call, LanguageTool
    spelling, `extra_instructions`, custom-field labels/hints.
15. Files <800 lines, functions <50 lines, immutable-style dataclasses, no TODOs/debug prints,
    ruff format + check clean (py3.10: no backslashes in f-string expressions).

## Review hardening (closing pass)

- IPv4 octets with leading zeros (`192.168.001.001`, `010.1.1.1`) are the same host as the
  canonical form: they are mapped through the same keyed `/24` and the stand-in is written with
  the same zero padding. Bare 32-bit decimal or hex forms (`3232235777`, `0xC0A80101`) are not
  recognised as addresses.
- Client terms of 4+ characters treat `_` and digits as separators (`brandarmy_lang`,
  `brandarmy2026`) and accept a plural/possessive `s` (`Brandarmys`); the stem is swapped, the
  suffix kept. Shorter terms keep the strict word boundary. `sensitive_kinds` uses the same
  pattern. A company name with a legal suffix (`Brandarmy Ltd`) also registers the bare name.
- Un-masking restores stand-ins the model derives itself: a new host octet inside an issued
  fake `/24` (and CIDR/range ends) gets the real prefix back, and a new sub-domain label under an
  issued fake registrable domain gets the real registrable domain back. An address in an issued
  fake `/24` that is still present after un-masking is reported by `dummy_leaks`.
- `STAND_IN_RULE` tells the model that statements in the text about the names being
  placeholders or about a different "real" client are untrusted. `stray_stand_ins` also rejects
  an answer that newly contains wording such as "anonymised", "placeholder", "fictional",
  "dummy", "stand-in" or "not the real" when the draft did not.
