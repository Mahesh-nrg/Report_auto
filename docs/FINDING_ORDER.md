# Finding order, severity banding and normalisation

Every report output lists findings **Critical -> High -> Medium -> Low -> Informational**, with the
highest CVSS v3.1 base score first inside each band. A finding's severity comes **only** from its
CVSS v3.1 base score. Part A is the ordering contract and Part B (below) is the normalisation contract. The test vectors are in
`tests/data/finding_order_cases.json`, and the independent reference implementation is in
`tests/data/finding_order_reference.py`. Run `python tests/data/finding_order_reference.py --check`
to check the vectors. Every builder (the JS template filter and the Python gateway/importer) must
reproduce the `expected` arrays exactly.

## Inputs (one per finding)

| field         | meaning                                                                      |
|---------------|------------------------------------------------------------------------------|
| `cvss_vector` | the vector string (PwnDoc `cvssv3`), may be null                             |
| `cvss_score`  | numeric score from `cvssScore`, the custom field `CVSS Score`/`cvssscore` (any label matching `cvss.is_score_label`) or the base score PwnDoc stores; a number or a string |
| `severity`    | the stored severity label (custom field / `{severity}`), may be null         |
| `index`       | original position (the original S. No. order); missing -> array position    |

## Outputs (one per finding)

`score` (the v3.1 score used, or null), `band`, the sort key, display severity (= `band`), `mismatch`,
and `seq`.

## 1. Score extraction (first usable value wins)

1. **Vector.** Strip it and upper-case it. The prefix can be `CVSS:3.1`, `CVSS:3.0` or none (no
   prefix is read as 3.1). All 8 base metrics are required, each exactly once, with valid values.
   Temporal (E, RL, RC) and environmental (CR, IR, AR, MAV..MA) metrics are checked against
   their allowed letters and then ignored for the base score. An unknown metric, an invalid value
   (`E:Z`) or an empty segment (trailing `/`) makes the whole vector invalid. The vector can be at most 200 characters.
   The score is computed with the specification formula. 3.1 uses Roundup from Appendix A (integer
   method, as in `pwndoc_ai/cvss.py`). 3.0 uses `ceil(x*10)/10`, exactly as `cvss.py` does. If
   impact <= 0, the score is 0.0.
2. **A non-v3 vector blocks the number.** If the vector has a `CVSS:` prefix other than 3.1/3.0
   (for example `CVSS:4.0/...`), the numeric score is assumed to belong to that version and is
   **not** used. The score is then null. A v4 vector or score alone never orders anything.
3. **Numeric score.** This step applies when there is no vector, or when the vector is invalid and
   has no prefix or a 3.x prefix.
   - A JSON number is used as is. Booleans, null, NaN, objects and lists are not usable.
   - A string is stripped. If it matches `^[0-9]{1,2},[0-9]$`, the comma is read as the
     decimal separator (`"7,5"` -> 7.5); `"1,234"` is unusable. It must then match `^[0-9]+(\.[0-9]+)?$`. So `"N/A"`,
     `"-"`, `""`, `"high"`, `"-1"` and `"7.5/10"` are all unusable.
   - The value must be within 0 <= x <= 10. `10.1` and `-2` are unusable.
   - The value is rounded to one decimal with **3.1 Roundup** (`7.45` -> 7.5, `8.95` -> 9.0,
     `0.01` -> 0.1).
4. If none of the above applies, the score is **null** here. This happens only on data that was not
   normalised: Part B always calculates a score (estimation), and the gateway and importer report
   `cvss_missing` until it does.

## 2. Band

- With a score: 0.0 -> `Informational`, 0.1-3.9 -> `Low`, 4.0-6.9 -> `Medium`, 7.0-8.9 -> `High`,
  9.0-10.0 -> `Critical`.
- Without a score: the stored label decides. It is stripped and matched without regard to case:
  `critical`, `high`, `medium`/`moderate`, `low`, `info`/`informational`/`none`
  (-> Informational). Anything else, including empty or null, gives `Unknown`.
- `mismatch` is true only when the score is not null, the label maps to a known band, and that band
  differs from the derived band. When they disagree, the **derived band wins** for both ordering and
  display/colouring. The gateway reports the disagreement (`severity_table_mismatch`-style issue).

## 3. Sort key (ascending, total order)

`(rank(band), score is null, -score, index, array position)` with rank Critical 0, High 1, Medium 2,
Low 3, Informational 4, Unknown 5. Within a band, scored findings come first by descending score.
Unscored findings come after them in their original order. Ties keep the original order (stable).

## 4. Numbering

`seq` = 1-based position after sorting, zero-padded to `max(3, digits(count))`: `001`... and
`0001`... once there are 1000 or more findings. The register table, the detail tables and the
`Finding 001 - <title>` headings all use this `seq`.

## 5. Consumers

Part A is a pure function. It can still see a null score, but only on data that was **not**
normalised (Part B always produces a score). It is used as is by:

- **PwnDoc template** (`ParadigmIT VAPT`): use `{#findings | sortFindings}` in the custom filter
  file (`scripts/paradigm/pwndoc_custom/report-filters-custom.js`). It returns new finding objects
  with `seq`, `orderScore`, `orderBand` and `severityMismatch` added, and never changes the input.
  Severity blocks, counts and colouring use `orderBand`. Use `seq` in place of
  `identifier | changeID`. The template cannot estimate (no LLM, no tables needed): a null-score
  finding sorts by its label as in section 3. After a Finish/Normalize write every finding has a
  vector, so template order == gateway order.
- **Gateway Word/PDF download:** runs the verification (Part B section 8) and returns it (JSON
  endpoint plus a short `X-Findings-Verification` header: `ok` or `issues=<n>`). If the audit is
  not normalised and the Settings switch **Normalise findings before download** is ON (default),
  the download first runs the same reviewed-plan write as Finish (backup first, per-audit lock),
  then exports. If the switch is OFF it exports in Part A order from a normalised **in-memory**
  copy (outputs are filled and ordered; PwnDoc is not touched) and reports the issues.
- **Finish / Normalize (gateway):** builds the Part B plan, shows the per-finding diff and
  verification in the dock, and writes back **only after the user approves**. It is one operation:
  automatic backup (restore feature) -> per-audit lock -> update every finding (vector, score,
  severity, CWE, CVE, N/A fills, `identifier`/order) -> re-read and re-verify -> unlock. If any
  step fails, it restores the backup.
- **DOCX importer:** normalises the plan (Part B) in the preview, shows the verification, and
  creates findings in sorted order. PwnDoc numbers new findings last+1, so creating them in this
  order *is* the renumbering. Then it verifies the created audit. Issue codes: `cvss_missing`
  (estimated), `severity_table_mismatch` (label != band), `cvss_v4_only`.

## Notes and edge decisions (Part A)

- Numbering in outputs is 3-digit (`001`); a 4th digit appears only at 1000+ findings.
- If the vector and the number are both usable and disagree, the vector wins silently (`cvss.py` is
  the source of truth).
- `"1,000"` and `"1,234"` are unusable (only one digit after a decimal comma). A score with more
  than one decimal (`"3.95"`) is still rounded with Roundup (-> 4.0), but verification reports
  `V13_score_not_canonical` as a warn so the change is visible.
- The test JSON ends with two generated cases (`generator: {seed, count}`, built with
  `finding_order_reference.generate`). Their findings and expected values are stored inline, so JS
  does not have to reproduce Python's RNG.

---

# Part B: normalisation ("all fields filled", missing CVSS calculated)

The test vectors for the deterministic path are in `tests/data/finding_normalize_cases.json`. Its
`tables` object holds the CWE table, the keyword rules, the label defaults and the informational
vector. It is the **single machine-readable source**: builders load it or copy it verbatim and
test against it. `finding_order_reference.normalize` is the reference implementation.
`--check` verifies both JSON files.

## B1. Abstract finding model

Input keys: `title, cvss_vector, cvss_score, severity, cwe, cve, location, path, parameter,
description, observation, poc, steps, remediation, references, affected, index` (any may be
missing). Output keys: the 11 text fields plus `seq, severity, cvss_score, cvss_vector,
cvss_source, cvss_estimated, cvss_v4_vector, cwe, cve, mismatch`.

Mapping to PwnDoc (adapters):

| abstract | PwnDoc |
|---|---|
| title | `title` |
| cvss_vector | `cvssv3` |
| cvss_score | custom field matching `cvss.is_score_label` (`CVSS Score`), else `cvssScore` |
| severity | custom field matching `cvss.is_severity_label`, else `{severity}` |
| cwe / cve | custom fields `CWE` / `CVE` (labels case-insensitive) |
| location, path, parameter | custom fields `Vulnerable Location`, `Path/URL`, `Parameter` |
| description, observation, poc, remediation | `description`, `observation`, `poc`, `remediation` (HTML) |
| steps | custom field `Steps to Reproduce` (HTML) |
| references | `references` (array of strings) |
| affected | `scope` |
| seq | `identifier` (written by Finish as the number) and the array order |

Adapters apply these rules. An HTML field filled with N/A becomes `<p>N/A</p>`. `references` is
judged on its joined, non-placeholder items; an empty result becomes `["N/A"]`. A custom field
that the audit type does not define is not created. It is reported as `field_absent`, which is a
warning, not a failure.

## B2. Placeholder rule (all text fields, CWE and CVE)

A value is a **placeholder** if:

- it is not a string (null, a list, a number), or
- its text is a placeholder after this cleanup: remove HTML tags, change `&nbsp;`/U+00A0 to a
  space, collapse whitespace, strip, lower-case. The placeholder texts are: empty, `-`, `--`,
  `---`, en dash, em dash, `none`, `null`, `nil`, `n/a`, `na`, `n.a.`, `tbd`, `undefined`,
  `not applicable`.

A placeholder becomes exactly `N/A`. Any other value is kept **unchanged** (no stripping, no
re-formatting). A non-string field value (for example `42`) is a placeholder, so it becomes
`N/A`. The `severity` field is not a text field (see B5).

## B3. CWE and CVE

- **CWE.** The `cwe` field is searched for `CWE[\s:_-]*(\d{1,5})` (case-insensitive, not inside a
  word). If there is no match and the field is only 1-5 digits, it is read as that id. Ids are
  written as `CWE-<int>` (leading zeros dropped), duplicates are removed keeping first-seen order,
  and the list is joined with `", "`. If the field gives no id, explicit `CWE-n` mentions in title,
  description, observation and references are used. If there are none, the matching keyword rule
  gives its CWE (B4). Otherwise the result is `N/A`. Unknown ids such as `CWE-9999` are **kept**;
  they just do not hit the table.
- **CVE.** Regex `CVE-(\d{4})-(\d{4,7})` (case-insensitive, upper-cased in the output). The `cve`
  field is searched first. If it gives nothing, the text of title, description, observation, poc
  and references is searched. Duplicates are removed keeping order, and ids are joined with
  `", "`. **A CVE is never invented.** Otherwise the result is `N/A`.
- An **Informational** result (B5) always has CWE = CVE = `N/A`, even if ids were given.
- Valid output formats: `N/A` | `CWE-\d+(, CWE-\d+)*` and `N/A` | `CVE-\d{4}-\d{4,7}(, ...)*`.

## B4. CVSS resolution (first match wins), source tag in `cvss_source`

1. **vector**: a valid v3.1/3.0/unprefixed vector (Part A step 1). `cvss_vector` is the canonical
   form: the `CVSS:3.1/` prefix (or `CVSS:3.0/` for 3.0), the 8 base metrics in AV..A order, and
   temporal/environmental metrics dropped. The score comes from the vector.
2. **numeric**: there is no v3 vector, the prefix is not v4/other, and the number is usable (Part
   A step 3). The score is kept and `cvss_vector` = `N/A` (the vector cannot be recovered from a
   score). Verification marks this as `warn` (V03). Exception: a score of 0.0 gets the
   informational vector.
3. **informational**: no usable score, and the stored label maps to Informational
   (`info`/`informational`/`none`). The result is `CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N`
   with score 0.0.
4. **estimated_llm** (gateway, LLM configured): the eight metrics are asked for with a strict
   tool schema. Each metric is an enum of its allowed letters (`AV: N|A|L|P`, `AC: L|H`,
   `PR: N|L|H`, `UI: N|R`, `S: U|C`, `C/I/A: H|L|N`). The input is title, description,
   observation, PoC, affected target and CWE, masked by the existing masking layer with
   realistic stand-ins. The answer is validated by `cvss.parse_vector`, and the score is
   **computed** by `cvss.py`; the model's arithmetic is never used. If the model returns an
   invalid answer, a timeout or an error, the next step is used. This step is not in the JSON
   vectors, because they are deterministic.
5. **estimated_heuristic**: the CWE table is checked first, using the first output CWE id that is
   in the table. Then the keyword rules are checked, in table order. Each keyword is matched as a
   lower-case substring not touching `[a-z0-9]` on either side, so `dos` does not match
   "windows". The title is tried against all rules first, then description + observation. If
   neither matches, the label default vector is used (by label band; `Unknown` if the label is
   missing). This step **always** returns a vector.

`cvss_estimated` is true for steps 4 and 5. The UI and report show "estimated by AI/heuristic -
review". A v4 vector is never scored. It is kept in `cvss_v4_vector` (stripped original), and the
v3.1 score comes from steps 3-5. Its numeric score is ignored, because it is a v4 score.

Then: `severity` = band of the score (Part A section 2, never `Unknown`), and
`mismatch` = (label band is known and differs from `severity`). Ordering and `seq` follow Part A
sections 3-4. The score is never null here, so the key is `(rank, -score, index, position)`.

## B5. Informational rule

A finding is Informational if and only if its resolved score is 0.0. That happens through a
vector with no impact, a numeric `0`, or the label rule in B4 step 3. It then gets: score 0.0,
the informational vector (unless a valid zero-impact vector was given, which is kept), and
CWE = CVE = `N/A`. A label of `Info` together with a positive usable score is **not**
Informational: the derived band wins and `mismatch` = true.

## B6. Why these vectors (CWE table)

The vectors follow the NVD/FIRST "typical" scoring for the weakness class as it is usually found
in web/API/mobile/network VAPT reports. The baseline is remote and unauthenticated unless the
class needs a session (IDOR, upload, mass assignment: PR:L) or a victim (XSS, CSRF, open
redirect, clickjacking: UI:R). AC:H is used where a network position or extra conditions are
needed (TLS, cookies, MitM, session issues). S:C is used where impact crosses into the browser or
internal network (XSS family, SSRF). They are starting points, and the finding is marked
estimated for review.

| CWE | name | vector (CVSS:3.1/...) | score | why |
|---|---|---|---|---|
| CWE-16 | Configuration | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | generic misconfiguration: remote, limited read |
| CWE-20 | Improper Input Validation | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N` | 6.5 | remote unauthenticated, limited C/I |
| CWE-22 | Path Traversal | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 | remote read of arbitrary files |
| CWE-77 | Command Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | remote code execution |
| CWE-78 | OS Command Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | remote code execution |
| CWE-79 | Cross-site Scripting | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 | reflected XSS reference vector (NVD typical) |
| CWE-80 | Basic XSS | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 | same as CWE-79 |
| CWE-89 | SQL Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | full database read/write, often OS |
| CWE-90 | LDAP Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | directory read/write |
| CWE-93 | CRLF Injection | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 | header injection leading to XSS / splitting |
| CWE-94 | Code Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | remote code execution |
| CWE-113 | HTTP Response Splitting | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 | as CRLF injection |
| CWE-200 | Information Exposure | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | remote limited confidentiality |
| CWE-204 | Response Discrepancy (user enumeration) | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | remote limited confidentiality |
| CWE-209 | Error Message Information Exposure | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | stack traces / verbose errors |
| CWE-269 | Improper Privilege Management | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H` | 8.8 | low-priv user gains full control |
| CWE-284 | Improper Access Control | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 8.1 | authenticated user reads/changes others data |
| CWE-285 | Improper Authorization | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 8.1 | as CWE-284 |
| CWE-287 | Improper Authentication | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N` | 9.1 | unauthenticated read/write |
| CWE-295 | Improper Certificate Validation | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N` | 7.4 | MitM, needs network position (AC:H) |
| CWE-306 | Missing Authentication for Critical Function | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N` | 9.1 | unauthenticated read/write |
| CWE-307 | Excessive Authentication Attempts | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | brute force, limited impact per attempt |
| CWE-311 | Missing Encryption of Sensitive Data | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N` | 5.9 | sniffing needs position (AC:H) |
| CWE-312 | Cleartext Storage | `AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 6.2 | local access to stored secrets (mobile) |
| CWE-319 | Cleartext Transmission | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N` | 5.9 | sniffing needs position (AC:H) |
| CWE-326 | Inadequate Encryption Strength | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N` | 3.7 | weak TLS ciphers, hard to exploit |
| CWE-327 | Broken Crypto Algorithm | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N` | 5.9 | decryption of protected data, AC:H |
| CWE-347 | Improper Signature Verification (JWT) | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N` | 9.1 | forged tokens -> auth bypass |
| CWE-352 | CSRF | `AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:H/A:N` | 6.5 | victim performs state change |
| CWE-384 | Session Fixation | `AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:H/A:N` | 6.8 | needs victim interaction and AC:H |
| CWE-400 | Uncontrolled Resource Consumption | `AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H` | 7.5 | remote availability loss |
| CWE-434 | Unrestricted File Upload | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H` | 8.8 | authenticated upload of executable content |
| CWE-502 | Deserialization of Untrusted Data | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | remote code execution |
| CWE-521 | Weak Password Requirements | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N` | 4.8 | guessable passwords, AC:H |
| CWE-522 | Insufficiently Protected Credentials | `AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N` | 6.5 | credential theft with user interaction |
| CWE-523 | Unprotected Transport of Credentials (HSTS) | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 | downgrade needs position |
| CWE-525 | Browser Cache of Sensitive Data | `AV:L/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 4.0 | local access to cached pages |
| CWE-548 | Directory Listing | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 | remote limited read |
| CWE-601 | Open Redirect | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 | NVD typical vector for CWE-601 |
| CWE-611 | XXE | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 | remote file read |
| CWE-613 | Insufficient Session Expiration | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N` | 4.8 | stolen session reused, AC:H |
| CWE-614 | Cookie without Secure flag | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 | sniffing needs position and interaction |
| CWE-639 | IDOR / BOLA | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N` | 6.5 | authenticated read of other users data |
| CWE-640 | Weak Password Recovery | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N` | 7.4 | account takeover, AC:H |
| CWE-644 | Host Header Injection | `AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N` | 5.4 | poisoned links need interaction |
| CWE-693 | Protection Mechanism Failure (missing headers) | `AV:N/AC:H/PR:N/UI:R/S:U/C:N/I:L/A:N` | 3.1 | defence-in-depth only |
| CWE-732 | Incorrect Permission Assignment | `AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 7.1 | local user modifies/reads files |
| CWE-770 | Allocation without Limits | `AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H` | 7.5 | remote availability loss |
| CWE-798 | Hard-coded Credentials / exposed keys | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 | remote read with the key |
| CWE-862 | Missing Authorization | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 8.1 | as CWE-284 |
| CWE-863 | Incorrect Authorization | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 8.1 | as CWE-284 |
| CWE-915 | Mass Assignment | `AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:H/A:N` | 6.5 | authenticated integrity change |
| CWE-916 | Weak Password Hash | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N` | 5.9 | offline cracking, AC:H |
| CWE-918 | SSRF | `AV:N/AC:L/PR:N/UI:N/S:C/C:L/I:L/A:N` | 7.2 | pivot into internal network, scope changed |
| CWE-942 | Permissive CORS | `AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:N/A:N` | 5.3 | credentialed cross-origin read with interaction |
| CWE-1004 | Cookie without HttpOnly | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 | as CWE-614 |
| CWE-1021 | Clickjacking | `AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:L/A:N` | 4.3 | UI redress needs interaction |
| CWE-1104 | Unmaintained Third-Party Components | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:L` | 5.6 | known flaws, exploit conditions vary |
| CWE-1336 | Server-Side Template Injection | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | remote code execution |
| CWE-1392 | Default Credentials | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 | full admin access |

**Keyword rules** (evaluated in this order; first match wins; title first, then description+observation):

| # | keywords | CWE | vector | score |
|---|---|---|---|---|
| 1 | sql injection, sqli, blind sql | CWE-89 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 2 | command injection, os command, remote code execution, rce, code execution | CWE-78 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 3 | template injection, ssti | CWE-1336 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 4 | deserialization, deserialisation | CWE-502 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 5 | ldap injection | CWE-90 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 6 | xxe, xml external entity | CWE-611 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 |
| 7 | ssrf, server-side request forgery, server side request forgery | CWE-918 | `AV:N/AC:L/PR:N/UI:N/S:C/C:L/I:L/A:N` | 7.2 |
| 8 | default credentials, default credential, default password, default community string | CWE-1392 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| 9 | authentication bypass, broken authentication, missing authentication, unauthenticated access | CWE-287 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N` | 9.1 |
| 10 | jwt, json web token, none algorithm | CWE-347 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N` | 9.1 |
| 11 | stored xss, persistent xss, stored cross-site scripting, stored cross site scripting | CWE-79 | `AV:N/AC:L/PR:L/UI:R/S:C/C:L/I:L/A:N` | 5.4 |
| 12 | xss, cross-site scripting, cross site scripting | CWE-79 | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 |
| 13 | path traversal, directory traversal, local file inclusion, lfi | CWE-22 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 |
| 14 | file upload, unrestricted upload | CWE-434 | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H` | 8.8 |
| 15 | privilege escalation | CWE-269 | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H` | 8.8 |
| 16 | idor, insecure direct object, bola, broken object level | CWE-639 | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N` | 6.5 |
| 17 | broken access control, missing authorization, missing authorisation, improper authorization, bfla, access control | CWE-285 | `AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N` | 8.1 |
| 18 | mass assignment | CWE-915 | `AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:H/A:N` | 6.5 |
| 19 | hardcoded, hard-coded, hard coded, api key exposed, exposed api key, secret key exposed | CWE-798 | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 |
| 20 | csrf, cross-site request forgery, cross site request forgery | CWE-352 | `AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:H/A:N` | 6.5 |
| 21 | open redirect, unvalidated redirect | CWE-601 | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 |
| 22 | crlf injection, response splitting | CWE-93 | `AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N` | 6.1 |
| 23 | host header injection, host header | CWE-644 | `AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N` | 5.4 |
| 24 | session fixation | CWE-384 | `AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:H/A:N` | 6.8 |
| 25 | session timeout, session expiration, session expiry, logout | CWE-613 | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N` | 4.8 |
| 26 | brute force, brute-force, rate limit, rate limiting, account lockout, captcha | CWE-307 | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| 27 | user enumeration, username enumeration, account enumeration | CWE-204 | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| 28 | weak password policy, password policy, weak password | CWE-521 | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N` | 4.8 |
| 29 | certificate pinning, ssl pinning, certificate validation | CWE-295 | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N` | 7.4 |
| 30 | smb signing | CWE-311 | `AV:A/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N` | 6.8 |
| 31 | insecure data storage, insecure storage, cleartext storage, plaintext storage | CWE-312 | `AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 6.2 |
| 32 | cleartext, clear text, unencrypted, plaintext, telnet, http instead of https | CWE-319 | `AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N` | 5.9 |
| 33 | weak cipher, weak ssl, weak tls, sslv3, sslv2, tls 1.0, tls 1.1, tlsv1.0, tlsv1.1, sweet32, poodle | CWE-326 | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N` | 3.7 |
| 34 | hsts, strict-transport-security | CWE-523 | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 |
| 35 | httponly, secure flag, samesite, cookie flag, cookie attribute | CWE-614 | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 |
| 36 | cors, cross-origin resource sharing | CWE-942 | `AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:N/A:N` | 5.3 |
| 37 | clickjacking, x-frame-options, frame-ancestors | CWE-1021 | `AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:L/A:N` | 4.3 |
| 38 | denial of service, dos, resource exhaustion | CWE-400 | `AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H` | 7.5 |
| 39 | directory listing, directory indexing | CWE-548 | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| 40 | stack trace, verbose error, error message, debug mode | CWE-209 | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| 41 | outdated, end of life, end-of-life, obsolete, vulnerable component, vulnerable version, unsupported version | CWE-1104 | `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:L` | 5.6 |
| 42 | information disclosure, information exposure, version disclosure, banner, introspection, sensitive data exposure | CWE-200 | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| 43 | browser cache, cache-control, autocomplete | CWE-525 | `AV:L/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 4.0 |
| 44 | security header, missing header, content-security-policy, x-content-type-options, referrer-policy | CWE-693 | `AV:N/AC:H/PR:N/UI:R/S:U/C:N/I:L/A:N` | 3.1 |

**Label default vectors** (no CWE hit, no keyword hit):

| stored label band | vector | score |
|---|---|---|
| Critical | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` | 9.8 |
| High | `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N` | 7.5 |
| Medium | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |
| Low | `AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N` | 3.1 |
| Unknown | `AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N` | 5.3 |

## B7. Normalisation order of operations

Per finding: fill text fields (B2) -> CWE/CVE ids (B3) -> CVSS (B4) -> band, mismatch ->
Informational rule (B5). Then for the whole list: sort and number (Part A sections 3-4). The
input objects are never changed; every step returns new objects.

## B8. Verification checks (per finding unless noted)

Each check returns `pass`, `fail`, `fixed` (normalisation changed it) or `warn`.

| id | check |
|---|---|
| V01 `fields_present` | every reported field is non-placeholder after normalisation (lists which were filled -> `fixed`) |
| V02 `vector_valid` | `cvss_vector` parses with `cvss.parse_vector` or is `N/A` with source `numeric` (`warn`) |
| V03 `score_matches_vector` | stored score == score computed from the vector (`fixed` when rewritten) |
| V04 `severity_matches_band` | stored label band == derived band (`fixed`; reports `severity_table_mismatch`) |
| V05 `cwe_format` | `N/A` or `CWE-n[, ...]` |
| V06 `cve_format` | `N/A` or `CVE-yyyy-n[, ...]`; no CVE that is absent from the source text |
| V07 `informational_rule` | score 0.0 => vector all-N impact, CWE = CVE = `N/A` |
| V08 `order_monotonic` (list) | rank ascending, score descending inside a band, ties by index |
| V09 `numbering_contiguous` (list) | `seq` = 001..N without gaps, matching the order; stored identifiers equal it after Finish |
| V10 `duplicate_title` (list) | case/whitespace-insensitive duplicate titles -> `warn` |
| V11 `estimated_review` | `cvss_estimated` true -> `warn` "estimated by AI/heuristic - review" |
| V12 `cvss_v4_ignored` | a v4 vector present -> `warn` (v3.1 was calculated) |

The report is JSON:
`{audit_id, normalised: bool, checks: [{id, finding_seq, title, status, detail}], summary: {pass, fail, fixed, warn}}`.
It is shown in the dock before Finish/download, and the importer shows it on the plan preview and
on the created audit.

## B9. Fill order for empty fields: AI enhance first (user rule)

User rule: "for the empty unfilled fields - first try to give them the appropriate values using
AI llm calls, internet searching - as the AI enhance field only". B2-B5 above describe only the
LAST, deterministic fallback. The full order for every field that is a placeholder (B2) is:

1. **AI fill (`fill_missing` mode of the Finish/Update refiner).** One LLM call per finding with
   the finding as context plus the research the refiner already gathers (context.py,
   web_context.py internet research, local RAG), query text redacted of client values, prompt
   masked with realistic stand-ins (MASKING_DESIGN.md), existing research cache, throttles,
   model fallback chain and cost limits respected. The tool schema lists ONLY the empty fields;
   filled fields are never sent for rewriting and any returned key that was not asked for is
   dropped. Per-field acceptance (anything failing validation is discarded, field stays empty):
   - description, observation/impact, PoC / steps to reproduce, remediation: non-placeholder
     text, unmasked, HTML-escaped into paragraphs.
   - references: URLs only, each must be one of the fetched research source URLs; invented URLs
     are removed.
   - CWE: `CWE-<digits>` present in the CWE list / NVD research; CVE: `CVE-yyyy-n` only when the
     finding text or a fetched source names it AND that source matches the finding.
   - location / URL / parameter / affected: only when the finding text already states the value
     (substring check after unmasking); never a guessed host, IP or parameter.
   - CVSS: eight letters, each from its allowed set; score computed by `cvss.parse_vector`, never
     the model's number; `cvss_source = "llm"`, `cvss_estimated = true`.
2. **Deterministic fallback** (B3 CWE from text, B4 heuristic vector, B5 informational rule),
   used when no LLM/internet is configured, the call failed, or a field came back invalid.
3. **`N/A`** for whatever is still empty (CWE/CVE `N/A`; informational score 0.0).

Every AI-filled value is recorded in `ai_filled: [field, ...]` with `ai_sources: {field: [url]}`
(same shape as the citations feature); the preview labels it "filled by AI" vs "N/A" and the user
can edit before apply. V01 reports AI fills as `fixed` with detail `ai`. Where it runs: gateway
Finish/Normalize (review-before-apply, automatic backup, per-audit lock); the importer (extends
its existing LLM gap fallback); downloads only when "Normalise findings before download" is ON
and an LLM is configured, otherwise straight to steps 2-3.

The JSON test vectors (`tests/data/finding_normalize_cases.json`) cover steps 2-3 only; the AI
step is tested with a fake LlmClient (accepted, rejected-invalid, unrequested-key, invented URL,
invented CVE, guessed host, call failure -> fallback).

## C. Integration notes (closer, 2026-10-26)

How the four parts fit together after integration, and the decisions taken for the gaps found.

**One order, three implementations, proven equal.** `src/pwndoc_ai/finding_order.py` (gateway,
B-NORMALIZE), `src/pwndoc_ai/docx_import/ordering.py` (importer) and the template filter
`sortFindings` (`scripts/paradigm/pwndoc_custom/report-filters-custom.js`) return identical
(title, score, band, seq, mismatch) rows for all 63 cases of `tests/data/finding_order_cases.json`
(incl. the generated 1500- and 250-finding cases) and 200 random lists from
`finding_order_reference.generate` (9,945 findings in total, 0 mismatches).

**Ties = stored S. No. everywhere.** The template filter breaks equal scores by the stored
identifier (`IDX-nnn`). The gateway plan (`finding_review._sort_key`) now does the same
(`identifier_index`, then stored position), because a Normalize write reorders the array but can
never change identifiers; with position-only ties the dock and the Word report numbered tied
findings differently.

**Estimated CVSS survives the write.** A finding custom field `CVSS Source` (input, added to the
installer's `CUSTOM_FIELDS`; created on the next `--update`) stores `vector`, `numeric`, or
`estimated_heuristic|estimated_llm <vector>`. The template filter reads it as `cvsssource` and
prints "(estimated by AI/heuristic - review)". The gateway keeps the finding flagged while the
stored vector still equals the estimated one; once a user edits the vector, it becomes `vector`.
Without that field in the audit type, the flag is shown only until the write (reported as
`field_absent`).

**Importer Part B.** `docx_import/normalize.py` applies the shared normaliser after the
importer's LLM fallback (AI first, B9) and before the order: an estimated v3.1 vector for unscored
findings (flag `cvss_estimated`), the derived band, CWE `CWE-n`/`N/A`, Informational 0.0,
`N/A` for empty parameter/location and `<p>N/A</p>` for empty rich text. The preview plan keeps
the extracted values (so completeness still lists the gaps) but is ordered by the normalised key;
the writer creates the normalised findings in exactly that order, so preview S. No. = PwnDoc
identifier. `verify_plan` reports on what will be created (`estimated` per row).

**Downloads.** With "Normalise findings before download" on (default), a download performs the
backup-protected Normalize write first (`X-Findings-Normalised: written`), then `ok` on later
downloads. Read-only users still get the file (`refused`, nothing written).
