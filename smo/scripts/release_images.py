#!/usr/bin/env python3
"""The images a release publishes (PR-OPS-4.2): one per distinct build in docker-compose.yml.

    python scripts/release_images.py matrix     # JSON for a GitHub Actions matrix
    python scripts/release_images.py names      # one image name per line

An image is `<registry>/<owner>/<repo>/smo-<name>:<version>`. The list comes from the compose file so a new module is
published without anyone editing the release workflow; `migrate` and `r1-termination` are one build and one image. The simulated O-DU of the
profile `oran-sim` (ADR 0012) is a build in the compose file too, but not an SMO image: it is built from O-RAN-SC's source by its own workflow
(.github/workflows/oran-sim-image.yml), so it is left out here (NOT_RELEASED). So are smo-teiv's two images of the profile `teiv` (plan task S3.23, ADR 0013): built
from O-RAN-SC smo-teiv's pinned tag with the named build context `teiv` by .github/workflows/teiv-image.yml, which publishes them as smo-teiv and smo-teiv-db.
"""

import json
import sys
from pathlib import Path

import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
# compose services with a build that the release does not publish: the O-RAN-SC simulator (ADR 0012) and smo-teiv (S3.23), each built from upstream's source by a workflow of its own
NOT_RELEASED = {"oran-sim-du", "teiv-db", "teiv-ingestion", "teiv-exposure"}


def images(compose: Path = SMO_ROOT / "docker-compose.yml") -> list[dict]:
    """[{name, context, module}] sorted by name; module is "" for a build with no MODULE argument (the GUI)."""
    found: dict[tuple[str, str], dict] = {}
    for name_of_service, service in (yaml.safe_load(compose.read_text()).get("services") or {}).items():
        build = service.get("build")
        if not build or name_of_service in NOT_RELEASED:
            continue
        context = build.get("context", ".")
        module = (build.get("args") or {}).get("MODULE", "")
        name = module.removeprefix("samples/") if module else Path(context).name
        found[(context, module)] = {"name": name, "context": context, "module": module}
    return sorted(found.values(), key=lambda i: i["name"])


def main(argv: list[str]) -> int:
    """Prints the image list in the form `argv[1]` asks for (`matrix`, the default: JSON `{"include": [...]}` for a GitHub Actions matrix; `names`: one name per line).
        Any other command prints the module docstring and returns 2.
    """
    command = argv[1] if len(argv) > 1 else "matrix"
    listed = images()
    if command == "matrix":
        print(json.dumps({"include": listed}))
    elif command == "names":
        print("\n".join(i["name"] for i in listed))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
