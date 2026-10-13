"""The PNG images of the call flows (docs/call-flows/images/, drawn by scripts/render_call_flows.py) match the diagrams they were drawn from: every
image in the manifest exists and its diagram text is still the document's, and a document that has images has one for each of its diagrams. No
browser runs here: the manifest keeps the SHA-256 of each diagram's text. Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration -q"""

import importlib.util
import json
from pathlib import Path

SMO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("render_call_flows", SMO / "scripts" / "render_call_flows.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)


def test_every_image_is_drawn_from_the_diagram_its_document_has_now():
    """Each manifest entry names an existing image whose diagram text is unchanged, and every diagram of an illustrated document has its image;
    re-run the script on the document when this fails."""
    manifest = json.loads(render.MANIFEST.read_text())
    documents = {key.split("/", 1)[0] for key in manifest}
    expected = {}
    for stem in documents:
        for name, _, source in render.diagrams(render.FLOWS / f"{stem}.md"):
            expected[f"{stem}/{name}"] = render.digest(source)
    assert manifest == expected, "re-render: python3 scripts/render_call_flows.py " + " ".join(f"docs/call-flows/{s}.md" for s in sorted(documents))
    for key in manifest:
        assert (render.IMAGES / key).is_file(), f"{key} is in the manifest but not in docs/call-flows/images/"


def test_a_changed_diagram_is_caught(tmp_path):
    """A diagram's digest changes with any edit to its text, so a stale image cannot pass."""
    assert render.digest("sequenceDiagram\n  A->>B: x\n") != render.digest("sequenceDiagram\n  A->>B: y\n")
