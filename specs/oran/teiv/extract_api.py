#!/usr/bin/env python3
"""Extracts the TE&IV OpenAPI document from Annex A (normative) of O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00 into TEIV_API_R005_v04.yaml (plan task S3.24).

    python specs/oran/teiv/extract_api.py ../smo-converged-inputs/oran-specs/O-RAN.WG10.TS.TEIV-API.0-R005-v04.00.pdf            # writes the file next to this script
    python specs/oran/teiv/extract_api.py <pdf> --check                                                                          # fails when the committed file differs

The specification is published as a PDF only (o-ran.org, behind a licence click-through); its Annex A carries the YAML as text in a monospace font
(Courier, 8 pt: 4.80 pt per character), indented by position, not by space characters. So the text is taken from the PDF's own text layer
(`pdftotext -bbox-layout`, poppler) and nothing is typed: every line's indentation and the spaces inside it are the x position of each word on that
4.80 pt grid from the annex's left margin (x = 42.60 pt). Then only extraction artefacts are fixed, each one a rule below, each application listed by
`--report` (and in specs/README.md):

  A1  page furniture: the footer ("(c) 2026 by the O-RAN ALLIANCE e.V. ..."), its page number and the header (the document id) at each page break are
      dropped (the text layer has no blank lines, so a page break adds none);
  A3  a blank line: the text layer has none, so a gap of n line pitches (9.65 pt) between two lines of one page is n - 1 blank lines (paragraph breaks
      inside `description: |` blocks are content); none is added across a page break, where the gap is the page's;
  A2  a line the PDF wrapped because it was wider than the page: its continuation starts at the left margin (column 0), where no YAML line of the document
      can start except a top-level key, and is joined to the line before it with one space.

A word that does not sit on the grid is an error (the extraction would have to guess), as is a document that does not parse as YAML afterwards.
"""

import argparse
import html
import re
import subprocess  # noqa: S404 — runs poppler's pdftotext with a fixed argument vector
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "TEIV_API_R005_v04.yaml"
DOC = "O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00"
LEFT, CHAR = 42.60, 4.80                    # the annex's left margin and the monospace character width, in points
FOOTER = re.compile(r"^© 2026 by the O-RAN ALLIANCE e\.V\.")
TOP_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:")
LINE = re.compile(r'<line xMin="([0-9.]+)" yMin="([0-9.]+)"[^>]*>(.*?)</line>', re.S)
PITCH = 9.65                                # the annex's line pitch, in points
WORD = re.compile(r'<word xMin="([0-9.]+)" yMin="[0-9.]+" xMax="([0-9.]+)"[^>]*>(.*?)</word>', re.S)
PAGE = re.compile(r"<page [^>]*>(.*?)</page>", re.S)


def text_lines(pdf: Path) -> list[tuple[int, str, bool, float]]:
    """(page, line text rebuilt on the character grid, on-grid, y) for every text line of the PDF, in reading order."""
    out = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout  # noqa: S603,S607
    lines = []
    for number, page in enumerate(PAGE.findall(out), start=1):
        for _, y, body in LINE.findall(page):
            words = [(float(x0), float(x1), html.unescape(w)) for x0, x1, w in WORD.findall(body)]
            if not words:
                continue
            text, on_grid, column = "", True, 0
            for x0, _x1, word in words:
                at = (x0 - LEFT) / CHAR
                if abs(at - round(at)) > 0.15:
                    on_grid = False
                start = max(round(at), column + (1 if text else 0))
                text += " " * (start - column) + word
                column = start + len(word)
            lines.append((number, text, on_grid, float(y)))
    return lines


def extract(pdf: Path) -> tuple[str, list[str]]:
    """The YAML of Annex A, and one line per artefact fixed (A1, A2)."""
    lines = text_lines(pdf)
    start = next(i for i, (_, t, _, _) in enumerate(lines) if " ".join(t.split()) == "openapi: 3.0.3")
    end = next(i for i in range(start, len(lines)) if " ".join(lines[i][1].split()).startswith("Annex (informative)"))
    fixes, out, last = [], [], (0, 0.0)
    for page, text, on_grid, y in lines[start:end]:
        if FOOTER.match(text.strip()):
            fixes.append(f"A1 page {page}: footer dropped")
            continue
        if text.strip().isdigit() and int(text.strip()) == page:
            fixes.append(f"A1 page {page}: page number dropped")
            continue
        if text.strip() == DOC:
            fixes.append(f"A1 page {page}: header dropped")
            continue
        if not on_grid:
            raise ValueError(f"page {page}: a word off the character grid, would have to guess its column: {text!r}")
        if last[0] == page and y - last[1] > 1.5 * PITCH:
            blanks = round((y - last[1]) / PITCH) - 1
            fixes.append(f"A3 page {page}: {blanks} blank line(s) restored before {text.strip()[:30]!r}")
            out += [""] * blanks
        last = (page, y)
        if out and text and not text[0].isspace() and not TOP_KEY.match(text):
            fixes.append(f"A2 page {page}: wrapped line joined: …{out[-1].strip()[-30:]!r} + {text.strip()[:30]!r}…")
            out[-1] = out[-1].rstrip() + " " + text.strip()
            continue
        out.append(text)
    return "\n".join(out).rstrip() + "\n", fixes


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("pdf", type=Path)
    p.add_argument("--check", action="store_true", help="compare with the committed file instead of writing it")
    p.add_argument("--report", action="store_true", help="print every fix")
    a = p.parse_args(argv)
    import yaml
    text, fixes = extract(a.pdf)
    doc = yaml.safe_load(text)
    ops = sum(1 for item in doc["paths"].values() for m in item if m in ("get", "put", "post", "delete", "patch"))
    if a.report:
        print("\n".join(fixes))
    print(f"{DOC}: OpenAPI {doc['openapi']}, info.version {doc['info']['version']}, {ops} operations, {len(fixes)} artefacts fixed")
    if a.check:
        if OUT.read_text() != text:
            print(f"{OUT.name} differs from the extraction of {a.pdf.name}", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
