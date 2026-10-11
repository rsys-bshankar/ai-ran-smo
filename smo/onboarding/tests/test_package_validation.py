"""Validation of package bytes (`app/package_validation.py`), which needs neither the database nor the web framework and which the offline conformance validator also runs.

The helper `csar` builds a small CSAR zip. Run: `cd smo/onboarding && PYTHONPATH=.:../shared python -m pytest tests/test_package_validation.py -q`.
"""

import io
import zipfile

import pytest

from app.package_validation import PackageValidationFailed, validate_package_bytes
from smo_shared.csar_signing import TrustStore, ZipLimits


def csar(**overrides) -> bytes:
    """Builds CSAR bytes with a TOSCA.meta and an ASD, plus `overrides` (keyword names use `__` for `/`; a value of None omits the entry)."""
    files = {"TOSCA-Metadata/TOSCA.meta": "Entry-Definitions: Definitions/main.yaml\n", "Definitions/main.yaml": "application_name: X\napplication_version: 1.0.0\nprovider: P\n"}
    files.update({k.replace("__", "/"): v for k, v in overrides.items()})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            if content is not None:
                z.writestr(name, content)
    return buf.getvalue()


def test_a_well_formed_package_gives_the_entry_artifacts_hash_and_identity():
    """A good package returns its entry definitions, the registered artifacts with their access URLs, a 64-character hash and the ASD identity."""
    entry, artifacts, integrity_hash, identity = validate_package_bytes(csar(Artifacts__Deployment__chart_tgz="x"), "http://h/p.csar")
    assert entry == "Definitions/main.yaml" and artifacts == [("Artifacts/Deployment/chart_tgz", "http://h/p.csar#Artifacts/Deployment/chart_tgz")]
    assert len(integrity_hash) == 64 and identity == {"name": "X", "version": "1.0.0", "vendor": "P"}


def test_a_tosca_meta_without_an_entry_definitions_line_is_a_validation_failure_not_a_stop_iteration():
    """A TOSCA.meta with no Entry-Definitions line raises PackageValidationFailed; an unguarded `next()` once raised StopIteration, which no caller catches.
    """
    with pytest.raises(PackageValidationFailed, match="no Entry-Definitions"):
        validate_package_bytes(csar(**{"TOSCA-Metadata__TOSCA.meta": "CSAR-Version: 1.1\n"}), "http://h/p.csar")


def test_a_location_that_does_not_end_in_csar_is_refused_here_too():
    """The `.csar` name check lives in the shared validation too, so the offline validator applies it as well."""
    with pytest.raises(PackageValidationFailed, match=r"does not end with \.csar"):
        validate_package_bytes(csar(), "http://h/p.zip")


def _deflated(files: dict) -> bytes:
    """Builds a deflate-compressed zip from `files` ({name: bytes or str}); a run of zeros of tens of megabytes is a few dozen kilobytes here, which is what a zip bomb is."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


_BASE = {"TOSCA-Metadata/TOSCA.meta": "Entry-Definitions: Definitions/main.yaml\n", "Definitions/main.yaml": "application_name: X\napplication_version: 1.0.0\nprovider: P\n"}


def test_an_unsigned_zip_bomb_is_refused_before_it_is_decompressed():
    """SEC-15.17: an unsigned package whose manifest.yaml inflates to 60 MiB from a few dozen kilobytes is refused with PackageValidationFailed under the default limits (it used to be read and parsed whole)."""
    bomb = _deflated({**_BASE, "manifest.yaml": b"\0" * (60 * 1024 * 1024)})
    assert len(bomb) < 200 * 1024
    with pytest.raises(PackageValidationFailed, match="too large to read"):
        validate_package_bytes(bomb, "http://h/p.csar")


def test_a_package_with_too_many_entries_is_refused(monkeypatch):
    """A package whose central directory lists more entries than the limit is refused before any file is read."""
    monkeypatch.setattr("app.package_validation.ZIP_LIMITS", ZipLimits(max_entries=5))
    with pytest.raises(PackageValidationFailed, match="entries, more than the 5 allowed"):
        validate_package_bytes(_deflated({**_BASE, **{f"Artifacts/{i}.txt": "x" for i in range(10)}}), "http://h/p.csar")


def test_a_file_over_the_member_limit_and_a_total_over_the_total_limit_are_refused(monkeypatch):
    """One file above `max_member_bytes`, and several files that are each under it but together above `max_total_bytes`, both fail the package; a package under both passes."""
    monkeypatch.setattr("app.package_validation.ZIP_LIMITS", ZipLimits(max_member_bytes=1000, max_total_bytes=1500))
    ok = {**_BASE, "manifest.yaml": "rappManifest: {manifestVersion: '1.0'}\n"}
    validate_package_bytes(_deflated(ok), "http://h/p.csar")
    with pytest.raises(PackageValidationFailed, match="1001 bytes uncompressed, more than the 1000 allowed"):
        validate_package_bytes(_deflated({**ok, "Artifacts/big.bin": b"a" * 1001}), "http://h/p.csar")
    with pytest.raises(PackageValidationFailed, match="more than 1500 bytes uncompressed"):
        validate_package_bytes(_deflated({**ok, "Artifacts/a.bin": b"a" * 800, "Artifacts/b.bin": b"b" * 800}), "http://h/p.csar")


def test_a_read_larger_than_the_member_limit_is_refused_even_if_the_directory_check_was_skipped(monkeypatch):
    """`_read` bounds the read itself, not only the directory: a file that really holds more than `max_member_bytes` is refused when read, so a directory that understates a size cannot get a big read through."""
    import app.package_validation as pv
    monkeypatch.setattr(pv, "ZIP_LIMITS", ZipLimits(max_member_bytes=100))
    with zipfile.ZipFile(io.BytesIO(_deflated({"a.txt": b"x" * 500, "b.txt": b"y" * 100}))) as z:
        with pytest.raises(PackageValidationFailed, match="larger than the 100 bytes allowed"):
            pv._read(z, "a.txt")
        assert pv._read(z, "b.txt") == b"y" * 100                      # exactly at the limit is fine


# One case per file the parse reads: each is made over a 300-byte member limit in turn.
@pytest.mark.parametrize("name", ["TOSCA-Metadata/TOSCA.meta", "Definitions/main.yaml", "manifest.yaml", "capabilities.yaml", "Files/Sme/providers/p.json", "Files/Sme/serviceapis/s.json"])
def test_every_file_the_parse_reads_goes_through_the_bounded_read(monkeypatch, name):
    """TOSCA.meta, the entry definitions, manifest.yaml, capabilities.yaml and the SME declarations are each refused when over the member limit, so none of the parse's reads is unbounded."""
    monkeypatch.setattr("app.package_validation.ZIP_LIMITS", ZipLimits(max_member_bytes=300, ratio_floor_bytes=10**9))
    content = {**_BASE, "manifest.yaml": "x: 1\n", "capabilities.yaml": "capabilities: {}\n", "Files/Sme/providers/p.json": "{}", "Files/Sme/serviceapis/s.json": "{}"}
    # the file is padded past the limit with a comment (YAML, TOSCA.meta) or a long string (JSON) so that it still parses and only the size is wrong
    content[name] = content[name] + "# " + "z" * 400 + "\n" if name.endswith((".yaml", ".meta")) else '{"k": "' + "z" * 400 + '"}'
    with pytest.raises(PackageValidationFailed, match="too large to read"):
        validate_package_bytes(_deflated(content), "http://h/p.csar")


def test_a_trust_store_without_required_signing_still_limits_an_unsigned_package(monkeypatch):
    """With a trust store and signing not required, the probe for signing entries opens the zip too: an unsigned package over the limits is refused there, not parsed."""
    monkeypatch.setattr("app.package_validation.ZIP_LIMITS", ZipLimits(max_entries=3))
    with pytest.raises(PackageValidationFailed, match="too large to read"):
        validate_package_bytes(_deflated({**_BASE, "a": "1", "b": "2"}), "http://h/p.csar", trust=TrustStore([]), require_signed=False)
