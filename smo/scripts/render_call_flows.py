#!/usr/bin/env python3
"""Renders the mermaid diagrams of call-flow documents to PNG, for readers who want an image (docs/call-flows/images/, OPEN_ITEMS.md DOC-CF).

    python3 scripts/render_call_flows.py docs/call-flows/01-rapp-onboarding-to-deployment.md [more.md ...]
    python3 scripts/render_call_flows.py sme/docs/lifecycle.md            (a module's lifecycle document, DOC-CF-2)

A document outside `docs/call-flows/` (a module's `docs/lifecycle.md`) gets its images and its own manifest beside it: `<module>/docs/images/<stem>/`
and `<module>/docs/images/manifest.json`.

Each mermaid block becomes `docs/call-flows/images/<document stem>/<section>.png`, named after the `## ` heading above it (`01a-onboard-a-package.png`),
drawn with the mermaid library the GUI already installs (`gui/node_modules/mermaid`, so `npm ci` in gui/ first) in Playwright's Chromium, at twice the
pixel density. `images/manifest.json` records, for every image, the SHA-256 of the diagram text it was drawn from;
`tests_integration/test_call_flow_images.py` fails when a document's diagram no longer matches its image, so a changed diagram is re-rendered in the
same change. Needs `pip install playwright` (Chromium: the one Playwright finds, or `CHROMIUM_PATH`).
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

SMO = Path(__file__).resolve().parent.parent
FLOWS = SMO / "docs" / "call-flows"
IMAGES = FLOWS / "images"
MANIFEST = IMAGES / "manifest.json"
MERMAID_JS = SMO / "gui" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
_BLOCK = re.compile(r"^## ([^\n]+)\n(?:(?!^## ).)*?```mermaid\n(.*?)```", re.S | re.M)  # the heading is one line: a `.+?` there let a section with no diagram borrow the next one


def diagrams(document: Path) -> list[tuple[str, str, str]]:
    """(file name, title, mermaid text) of every diagram of a document, named after the `## ` heading above it; a second diagram under the same
    heading gets `-2`, `-3`."""
    out, seen = [], {}
    for heading, source in _BLOCK.findall(document.read_text()):
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:60]
        seen[slug] = seen.get(slug, 0) + 1
        name = f"{slug}{'' if seen[slug] == 1 else '-' + str(seen[slug])}.png"
        out.append((name, heading, source))
    return out


def digest(source: str) -> str:
    """The SHA-256 of a diagram's text, as the manifest records it."""
    return hashlib.sha256(source.encode()).hexdigest()


def _page(title: str, source: str, library: str) -> str:
    """A page that draws one diagram under its title and marks the body when mermaid has finished (or failed)."""
    escaped = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<html><head><style>body{{margin:0;padding:24px;background:#fff;font-family:Arial,sans-serif}}h1{{font-size:22px;margin:0 0 12px}}</style>
<script>{library}</script></head><body><h1>{title}</h1><pre class="mermaid">{escaped}</pre>
<script>mermaid.initialize({{startOnLoad:false, theme:'default', sequence:{{useMaxWidth:false}}}});
mermaid.run().then(()=>document.body.dataset.done='1').catch(e=>{{document.body.dataset.done='error: '+e}});</script></body></html>"""


def images_of(document: Path) -> tuple[Path, Path, str]:
    """(images folder, manifest, title prefix) of a document: the call flows share `docs/call-flows/images/`; any other document (a module's lifecycle,
    DOC-CF-2) has `images/` and its own manifest beside it, and its titles name the module."""
    if document.parent == FLOWS:
        return IMAGES, MANIFEST, "Call flow"
    module = document.parent.parent.name
    return document.parent / "images", document.parent / "images" / "manifest.json", f"{module} {document.stem}"


def render(documents: list[Path]) -> None:
    """Draws every diagram of the documents and updates their manifests; exits with the mermaid error when one does not render."""
    from playwright.sync_api import sync_playwright

    library = MERMAID_JS.read_text()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        page = browser.new_page(viewport={"width": 2400, "height": 1200}, device_scale_factor=2)
        for document in documents:
            images, manifest_path, prefix = images_of(document)
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            folder = images / document.stem
            folder.mkdir(parents=True, exist_ok=True)
            # the document is drawn whole: images of diagrams it no longer has (a heading renamed or removed) go, from the folder and the manifest
            for stale in folder.glob("*.png"):
                stale.unlink()
            manifest = {key: value for key, value in manifest.items() if not key.startswith(f"{document.stem}/")}
            for name, title, source in diagrams(document):
                page.set_content(_page(f"{prefix} {title}", source, library))
                page.wait_for_function("document.body.dataset.done")
                state = page.evaluate("document.body.dataset.done")
                if state != "1":
                    raise SystemExit(f"{document.name} {title}: {state}")
                page.screenshot(path=str(folder / name), full_page=True)
                manifest[f"{document.stem}/{name}"] = digest(source)
                print(f"wrote {(folder / name).relative_to(SMO)}")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + "\n")
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    render([Path(a).resolve() for a in sys.argv[1:]])
