#!/usr/bin/env python3
"""Zips a sample rApp directory into <name>.csar — the actual package
Onboarding's `_validate_package` fetches over HTTP and opens.

    python3 smo/samples/build_csar.py                      # every sample
    python3 smo/samples/build_csar.py energy-saving-rapp   # one sample
    python3 smo/samples/build_csar.py --key my.pem NAME    # sign with your own key instead of the demo one
    python3 smo/samples/build_csar.py --unsigned NAME      # no digest list, no signature
    python3 smo/samples/build_csar.py --source-dir <dir> --name <name>   # any package directory -> ./<name>.csar, signed the same way
                                                           # (the Java example: sdk-java/README.md)

Test suites, caches, the sample's own service tests and its README.md are left out, and
every entry carries a fixed timestamp, so a rebuild of unchanged sources is
byte-identical (ed25519 signing is deterministic).

PR-RAPP-1: the package carries a digest list of every file (TOSCA-Metadata/DIGESTS.sha256) and a detached ed25519
signature over it (TOSCA-Metadata/DIGESTS.sha256.sig, smo_shared/csar_signing.py). By default the key is the DEMO publisher's
(samples/demo-signing/): its private half is committed on purpose and must never be trusted in production. To sign your own
rApp package, `scripts/csar_sign.py`.

See smo/DEMO_RUNBOOK.md for how to serve the resulting .csar file and
walk through the full rApp lifecycle against a running `docker compose
up` stack.
"""

import argparse
import io
import sys
import zipfile
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SAMPLES_DIR.parent / "shared"))      # smo_shared, when it is not installed

from smo_shared import csar_signing  # noqa: E402

DEMO_KEY = SAMPLES_DIR / "demo-signing" / "demo-publisher.seed"       # DEMO MATERIAL: the private half is public on purpose (demo-signing/README.md)
SAMPLES = ["energy-saving-rapp", "mobility-optimization-rapp", "coverage-optimization-rapp",
           "traffic-steering-rapp"]
EXCLUDED_PARTS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDED_FILES = {"README.md", "module.yaml"}  # sample documentation, and the module manifest (how the SMO repository wires the sample: scripts/modules.py), not package content
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def source_files(name: str, source: Path | None = None) -> dict[str, bytes]:
    """The files of `samples/<name>/`, or of the directory `source` when given."""
    source = source or SAMPLES_DIR / name
    files = {}
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if path.is_file() and not EXCLUDED_PARTS & set(rel.parts) and rel.as_posix() not in EXCLUDED_FILES:
            files[rel.as_posix()] = path.read_bytes()
    return files


def build_bytes(name: str, key: Path | None = DEMO_KEY, *, sign: bool = True, source: Path | None = None) -> bytes:
    """The package as bytes. Signed by default, with `key` (the demo publisher's unless another file is given); `sign=False` writes no digest list and no signature. `source` is a package directory outside `samples/` (the Java SDK's example)."""
    files = source_files(name, source)
    if sign:
        files |= csar_signing.signing_entries(files, csar_signing.load_private_key(Path(key or DEMO_KEY).read_bytes()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, content in files.items():
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
    return buf.getvalue()


def build(name: str, key: Path | None = DEMO_KEY, *, sign: bool = True) -> Path:
    """Writes the signed (or, with `sign=False`, unsigned) package of the named sample to `samples/<name>.csar` and prints its path; this is the
    file the integration suite compares with the sample's sources.
    """
    output = SAMPLES_DIR / f"{name}.csar"
    output.write_bytes(build_bytes(name, key, sign=sign))
    print(f"wrote {output.relative_to(SAMPLES_DIR.parent.parent)}" + ("" if sign else " (unsigned)"))
    return output


def build_dir(source: Path, name: str, key: Path | None = DEMO_KEY, *, sign: bool = True) -> Path:
    """A package directory outside `samples/`: `./<name>.csar`, built the same way."""
    output = Path.cwd() / f"{name}.csar"
    output.write_bytes(build_bytes(name, key, sign=sign, source=source.resolve()))
    print(f"wrote {output}" + ("" if sign else " (unsigned)"))
    return output


def main(argv: list[str] | None = None) -> None:
    """Command line of the builder: builds the named samples (all four by default) with the demo key or `--key`, or, with `--source-dir` and
    `--name`, a package directory outside `samples/` into `./<name>.csar`.
    """
    parser = argparse.ArgumentParser(description="Zip a sample rApp into <name>.csar, signed.")
    parser.add_argument("names", nargs="*", help="samples to build (default: all four)")
    parser.add_argument("--key", type=Path, help="the private key to sign with (PEM or ed25519-seed line); default: the DEMO publisher's")
    parser.add_argument("--unsigned", action="store_true", help="write no digest list and no signature")
    parser.add_argument("--source-dir", type=Path, help="build this package directory instead of a sample (needs --name)")
    parser.add_argument("--name", help="the package name for --source-dir: writes ./<name>.csar")
    args = parser.parse_args(argv)
    if args.source_dir or args.name:
        if not (args.source_dir and args.name) or args.names:
            parser.error("--source-dir and --name go together and replace the sample names")
        build_dir(args.source_dir, args.name, args.key or DEMO_KEY, sign=not args.unsigned)
        return
    for name in args.names or SAMPLES:
        build(name, args.key or DEMO_KEY, sign=not args.unsigned)


if __name__ == "__main__":
    main()
