"""Builds small DOCX packages shaped like reports from the active PwnDoc template, in memory.

Raw WordprocessingML strings only (no python-docx), so every structure the reader must handle
(merged cells, numbering, text boxes, tracked changes, hidden runs, content controls, pictures)
can be written exactly. Nothing here is a real client document.
"""

from __future__ import annotations

import io
import struct
import zipfile
from html import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAMESPACES = (
    f'xmlns:w="{W}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_REL = f"{REL}/image"
BULLET_NUM, DECIMAL_NUM = "1", "2"


def png(width: int = 640, height: int = 480, pad: int = 400) -> bytes:
    """A PNG signature and IHDR chunk with the given size (enough for sniffing)."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0" * (4 + pad)


def jpeg(width: int = 800, height: int = 600, pad: int = 400) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\0" * pad + b"\xff\xd9"


def gif(width: int = 300, height: int = 200, pad: int = 400) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\0" * pad


GIF_1X1 = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00"


# ---------------------------------------------------------------- runs and paragraphs


def run(text: str, *, bold: bool = False, hidden: bool = False) -> str:
    props = ("<w:b/>" if bold else "") + ("<w:vanish/>" if hidden else "")
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    pieces = text.split("\n")
    body = "<w:br/>".join(
        f'<w:t xml:space="preserve">{escape(p, quote=False)}</w:t>' for p in pieces
    )
    return f"<w:r>{rpr}{body}</w:r>"


def para(*runs: str, num: str = "", level: int = 0, style: str = "") -> str:
    props = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num:
        props += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def p(text: str = "", **kwargs) -> str:
    bold = kwargs.pop("bold", False)
    return para(run(text, bold=bold) if text else "", **kwargs)


def picture(rid: str) -> str:
    """A paragraph holding one inline picture."""
    blip = f'<a:blip r:embed="{rid}"/>'
    graphic = (
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<pic:pic><pic:blipFill>{blip}</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
    )
    return f'<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="x"/>{graphic}</wp:inline></w:drawing></w:r></w:p>'


def textbox(*paragraphs: str, fallback: str = "") -> str:
    """A cover text box: the mc:Choice copy, and a (possibly different) mc:Fallback copy."""
    inner = "".join(paragraphs)
    other = fallback or inner
    return (
        "<w:p><w:r><mc:AlternateContent>"
        f'<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:wsp><wps:txbx><w:txbxContent>{inner}'
        "</w:txbxContent></wps:txbx></wps:wsp></wp:anchor></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>{other}</w:txbxContent>"
        "</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )


# ---------------------------------------------------------------- tables


def cell(*paragraphs: str, span: int = 1, vmerge: str = "") -> str:
    props = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    if vmerge:
        props += f'<w:vMerge w:val="{vmerge}"/>' if vmerge == "restart" else "<w:vMerge/>"
    tcpr = f"<w:tcPr>{props}</w:tcPr>" if props else ""
    return f"<w:tc>{tcpr}{''.join(paragraphs) or '<w:p/>'}</w:tc>"


def row(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def table(*rows: str) -> str:
    return f"<w:tbl><w:tblPr/>{''.join(rows)}</w:tbl>"


def texts(value) -> str:
    """Paragraphs for a value: a str (one paragraph) or a list of paragraph XML strings."""
    if isinstance(value, str):
        return p(value)
    return "".join(value)


def detail_table(
    ident: str,
    title: str,
    *,
    location: str = "",
    urls: str = "",
    parameter: str = "",
    cwe: str = "",
    cvss: str = "",
    severity: str = "",
    description=(),
    poc=(),
    remediation=(),
    references=(),
) -> str:
    """The template's 12-row "Detailed Technical Summary" table."""
    return table(
        row(cell(p("S. No.")), cell(p(f" {ident} "), span=2)),
        row(cell(p("Name of Vulnerability: ")), cell(p(f" {title}"), span=2)),
        row(
            cell(p("\t\t\tVulnerable Point / Error "), p("Location "), vmerge="restart"),
            cell(p("Vulnerable Location")),
            cell(p(location)),
        ),
        row(cell(vmerge="continue"), cell(p("Vulnerable Path/ Port/ URL ")), cell(texts(urls))),
        row(cell(vmerge="continue"), cell(p("Vulnerable Parameter ")), cell(p(parameter))),
        row(cell(p("CVE/CWE  ")), cell(p(cwe), span=2)),
        row(cell(p("CVSS Score")), cell(p(cvss), span=2)),
        row(cell(p("Severity")), cell(p(severity), span=2)),
        row(cell(p("Description ")), cell(texts(description), span=2)),
        row(cell(p("Proof of concept:", bold=True), texts(poc), span=3)),
        row(cell(p("Recommendations ")), cell(texts(remediation), span=2)),
        row(cell(p("References  ")), cell(texts(references), span=2)),
    )


def summary_table(*entries: tuple[str, str, str, str]) -> str:
    """(id, blob with URL:/Parameter:/Location: lines, title, cwe) per finding."""
    head = row(
        cell(p("S. No. ")),
        cell(p("Vulnerable location, "), p("parameter, path, link ")),
        cell(p("Name of the vulnerability ")),
        cell(p("References (CVEID, CWE-ID ) ")),
    )
    body = [
        row(
            cell(p(""), p(ident)),
            cell(*[p(line) for line in blob.split("\n")]),
            cell(p(title)),
            cell(p(cwe)),
        )
        for ident, blob, title, cwe in entries
    ]
    return table(head, *body)


def front_tables(company: str, scope: list[str], start: str, end: str) -> str:
    application = table(
        row(cell(p("S. No")), cell(p("Name of the Application")), cell(p("Test Type"))),
        row(cell(p("1")), cell(p(company)), cell(p("Grey Box Penetration Testing"))),
    )
    scope_table = table(
        row(cell(p("S. No")), cell(p("Scope URL"))),
        row(cell(p("1")), cell(*[p(s) for s in scope])),
    )
    dates = table(
        row(cell(p("Start Date")), cell(p("End Date"))),
        row(cell(p(start)), cell(p(end))),
    )
    return application + scope_table + dates


def severity_table(counts: dict[str, str]) -> str:
    names = ("Critical", "High", "Medium", "Low", "Informational")
    return table(
        row(cell(p("Risk Severity")), *[cell(p(n)) for n in names]),
        row(cell(p("No. of vulnerabilities")), *[cell(p(counts.get(n, ""))) for n in names]),
    )


def disclaimer(company: str, tested: str = "undefined") -> str:
    return p("Disclaimer", style="Heading1") + p(
        f"This report was prepared by ParadigmIT Cyber for the exclusive benefit of {company} and "
        f"is proprietary information. The report was delivered to {company}. The present "
        f"applications are tested as of {tested}."
    )


# ---------------------------------------------------------------- package


NUMBERING = (
    f'<w:numbering xmlns:w="{W}">'
    '<w:abstractNum w:abstractNumId="10"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="20"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:numFmt w:val="lowerLetter"/></w:lvl></w:abstractNum>'
    f'<w:num w:numId="{BULLET_NUM}"><w:abstractNumId w:val="10"/></w:num>'
    f'<w:num w:numId="{DECIMAL_NUM}"><w:abstractNumId w:val="20"/></w:num>'
    "</w:numbering>"
)


def package(
    body: str,
    *,
    media: dict[str, bytes] | None = None,
    header: str | None = None,
    extra: dict[str, bytes] | None = None,
    rels: str = "",
) -> bytes:
    """A .docx with ``body`` as the document body. ``media`` maps rIdN -> bytes (stored as
    word/media/imageN.png whatever the real type, as the generator does)."""
    media = media or {}
    links = [
        f'<Relationship Id="rNum" Type="{REL}/numbering" Target="numbering.xml"/>',
        f'<Relationship Id="rHdr" Type="{REL}/header" Target="header1.xml"/>',
    ]
    files = {
        "[Content_Types].xml": (
            b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Default Extension="png" ContentType="image/png"/>'
            b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            b'officedocument.wordprocessingml.document.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        ).encode(),
        "word/document.xml": f'<?xml version="1.0"?><w:document {NAMESPACES}><w:body>{body}<w:sectPr/></w:body></w:document>'.encode(),
        "word/numbering.xml": NUMBERING.encode(),
        "word/header1.xml": (
            f"<w:hdr {NAMESPACES}>{header if header is not None else p('')}</w:hdr>"
        ).encode(),
    }
    for index, (rid, data) in enumerate(media.items(), start=1):
        files[f"word/media/image{index}.png"] = data
        links.append(
            f'<Relationship Id="{rid}" Type="{IMAGE_REL}" Target="media/image{index}.png"/>'
        )
    files["word/_rels/document.xml.rels"] = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(links)
        + rels
        + "</Relationships>"
    ).encode()
    files.update(extra or {})
    return zip_of(files)


def zip_of(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return out.getvalue()


def header_line(company: str) -> str:
    return para(run(company), run(" " * 40), run("ParadigmIT Cybersecurity"))


def report(
    findings: list[str],
    *,
    company: str = "Acme Corp",
    summary: str = "",
    media: dict[str, bytes] | None = None,
    start: str = "05.03.2026",
    end: str = "06.03.2026",
    scope: tuple[str, ...] = ("https://acme.example", "https://api.acme.example/"),
    leftover: bool = True,
    severity_counts: dict[str, str] | None = None,
    after: str = "",
    tested: str = "undefined",
    disclaimer_company: str = "",
) -> bytes:
    """A whole report: cover box, executive summary (with the template's leftover name), front
    tables, the summary table, the detail tables and the disclaimer."""
    cover = textbox(
        p(f"Vulnerability Assessment and Penetration Testing Report - {company}"),
        p("By"),
        p("ParadigmIT Cyber"),
    )
    executive = p("Executive Summary", style="Heading1") + p(
        "This report presents the results of the penetration testing for "
        + ("Alfred AI" if leftover else company)
        + " web applications."
    )
    body = (
        cover
        + executive
        + severity_table(severity_counts or {})
        + front_tables(company, list(scope), start, end)
        + p("List of Vulnerable Parameter, Location discovered", style="Heading1")
        + summary
        + p("Detailed Technical Summary", style="Heading1")
        + "".join(findings)
        + after
        + disclaimer(disclaimer_company or company, tested)
    )
    return package(body, media=media, header=header_line(company))
