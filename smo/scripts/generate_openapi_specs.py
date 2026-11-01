#!/usr/bin/env python3
"""Regenerates smo/docs/openapi/<module>.json for every module from its own
live FastAPI app.openapi() output.

HISTORY.md §2: "No persisted OpenAPI spec files anywhere — relying
entirely on FastAPI's live /docs generation rather than committed contracts."
These files ARE that live generation, just frozen to disk so a contract
change shows up as a diff in review rather than only at runtime.
`tests_integration/test_openapi_specs.py` fails CI if a committed file drifts
from what the app would generate today — run this script and commit the
result to fix that.
"""

import json
import os
import sys
from pathlib import Path

# Importing the apps builds the database engine but never connects; there is no default URL (PR-DB-1).
os.environ.setdefault("SMO_DATABASE_URL", "sqlite://")

SMO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO_ROOT / "tests_integration"))

from loader import load_app_module  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modules  # noqa: E402  (scripts/modules.py: the module manifests)

ALL_MODULES = modules.openapi_modules()   # the modules whose module.yaml says `openapi: true`
sys.path.insert(0, str(SMO_ROOT / "shared"))
sys.path.insert(0, str(SMO_ROOT / "sdk"))


def generate(module_dir: str) -> dict:
    """The module's OpenAPI document as its FastAPI app would serve it now (the app is imported by `tests_integration/loader.py`, which does not start it)."""
    main = load_app_module(module_dir)
    return main.app.openapi()


def main() -> None:
    """Writes `docs/openapi/<module>.json` (sorted keys, two-space indent) for every module in `ALL_MODULES`, replacing the existing file, and prints each path.

        Run it after changing a route or a request/response model, then commit the result; `tests_integration/test_openapi_specs.py` fails when the committed files differ.
    """
    out_dir = SMO_ROOT / "docs" / "openapi"
    out_dir.mkdir(parents=True, exist_ok=True)
    for module_dir in ALL_MODULES:
        spec = generate(module_dir)
        out_path = out_dir / f"{Path(module_dir).name}.json"
        out_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path.relative_to(SMO_ROOT)}")


if __name__ == "__main__":
    main()
