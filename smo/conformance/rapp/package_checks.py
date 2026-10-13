"""The offline checks: a rApp package (a CSAR) against what Onboarding will do with it. Nothing is running; nothing is fetched.

PK-V is the verdict: it calls `onboarding/app/package_validation.py`, the code `POST /packages` runs, so a package that passes it onboards (the NFO call that
follows is the one thing it does not exercise). The others take the same parsers apart so a developer is told which file is wrong, and add advice Onboarding does
not give (a warning is not a refusal).
"""

import importlib.util
import io
import re
import sys
import zipfile
from pathlib import Path

from smo_shared import csar_signing

from .kit import PACKAGE, Fail, PackageContext, Skip, Warn, check

ONBOARDING_VALIDATION = Path(__file__).resolve().parents[2] / "onboarding" / "app" / "package_validation.py"
SDK_NAMESPACES = {"data", "analytics", "models", "lifecycle", "intent", "platform"}
ASD_IDENTITY = ("application_name", "application_version", "provider")
NOT_SHIPPED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", "tests", "node_modules"}
# File names that are never meant to travel in a package: environment files and key or keystore files.
SECRET_NAMES = re.compile(r"(^|/)(\.env|id_rsa|id_ed25519|.*\.(pem|key|p12|pfx|jks))$", re.I)
# A PEM private key header, or the `ed25519-seed:<base64>` form of the SMO's own signing keys.
PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|ed25519-seed:[A-Za-z0-9+/]{43}=")
# Files above this size are not scanned for PK-9, so a key inside a larger file is not found.
MAX_SCANNED_BYTES = 1_000_000


def onboarding():
    """Onboarding's own validation module (`onboarding/app/package_validation.py`), loaded from its file and cached in `sys.modules`.

    It is loaded by path because every SMO module's Python package is called `app`, so it cannot be imported by name next to this kit. The checks call its parsers
    (`_asd_identity`, `_parse_ai_capabilities`, `_parse_sme_declarations`, `validate_package_bytes`), which makes PK-V the same code `POST /packages` runs, and also means a
    rename of those private functions breaks the kit.
    """
    module = sys.modules.get("rapp_onboarding_package_validation")
    if module is None:
        spec = importlib.util.spec_from_file_location("rapp_onboarding_package_validation", ONBOARDING_VALIDATION)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _open(ctx: PackageContext) -> zipfile.ZipFile:
    """The package as a `ZipFile`, or `Skip` (naming PK-1) when it is not a zip or is over Onboarding's zip limits, so one broken file fails PK-1 once and the later checks skip.

    SEC-15.17b: it opens the zip with Onboarding's own `_open_zip`, so the entry-count, size and compression-ratio limits (`onboarding().ZIP_LIMITS`, the ones
    `csar_signing.verify_zip` applies) are checked from the directory before anything is decompressed, and every read in this module goes through `_read`.
    """
    module = onboarding()
    try:
        return module._open_zip(ctx.data)
    except zipfile.BadZipFile:
        raise Skip("the package is not a readable zip file (PK-1)") from None
    except module.PackageValidationFailed as exc:
        raise Skip(f"the package is over the zip limits Onboarding applies (PK-1): {exc}") from None


def _read(z: zipfile.ZipFile, name: str) -> bytes:
    """One file of the package, read the way Onboarding reads it (bounded by `ZIP_LIMITS.max_member_bytes`); `KeyError` for a missing file, `PackageValidationFailed` for a file that is too big."""
    return onboarding()._read(z, name)


def _verify_crcs(z: zipfile.ZipFile) -> None:
    """`ZipFile.testzip` without its unbounded reads: streams every file in chunks, so the CRC is checked at the end of each, and stops with `Fail` once a file really holds more
    than `ZIP_LIMITS.max_member_bytes` or the files together more than `max_total_bytes` (a directory that understates a size cannot make this read a bomb in full)."""
    limits = onboarding().ZIP_LIMITS
    total = 0
    for info in z.infolist():
        if info.is_dir():
            continue
        size = 0
        try:
            with z.open(info) as member:
                while chunk := member.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > limits.max_member_bytes or total > limits.max_total_bytes:
                        raise Fail(f"package too large to read: {info.filename[:80]!r} really holds more than the zip limits allow ({limits.max_member_bytes} bytes a file, {limits.max_total_bytes} in all)")
        except zipfile.BadZipFile:
            raise Fail(f"entry {info.filename} is corrupt (its CRC does not match)") from None


def _scanned(z: zipfile.ZipFile, name: str) -> bytes:
    """The bytes PK-9 scans: a file that really holds more than `MAX_SCANNED_BYTES` (its directory entry understated it) is not read past that, and is not scanned."""
    try:
        return csar_signing.read_member(z, name, MAX_SCANNED_BYTES)
    except csar_signing.SignatureError:
        return b""


def _only(z: zipfile.ZipFile, *names: str) -> zipfile.ZipFile:
    """A zip holding just these entries of `z` that exist: the Onboarding parsers read a whole package, a check wants one file's verdict."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for name in names:
            if name in z.namelist():
                target.writestr(name, _read(z, name))
    return zipfile.ZipFile(io.BytesIO(out.getvalue()))


def _asd_definitions(z: zipfile.ZipFile) -> tuple[str, str]:
    """`(path, text)` of the entry definitions file that TOSCA.meta names; `Fail` when TOSCA.meta, its `Entry-Definitions:` line or the file it names is missing."""
    try:
        meta = _read(z, "TOSCA-Metadata/TOSCA.meta").decode()
    except KeyError:
        raise Fail("TOSCA-Metadata/TOSCA.meta is missing") from None
    except onboarding().PackageValidationFailed as exc:
        raise Fail(str(exc)) from None
    entry = next((line.split(":", 1)[1].strip() for line in meta.splitlines() if line.startswith("Entry-Definitions:")), None)
    if not entry:
        raise Fail("TOSCA-Metadata/TOSCA.meta has no Entry-Definitions: line")
    try:
        return entry, _read(z, entry).decode(errors="replace")
    except KeyError:
        raise Fail(f"the entry definitions file {entry} named by TOSCA.meta is not in the package") from None
    except onboarding().PackageValidationFailed as exc:
        raise Fail(str(exc)) from None


@check("PK-1", "STRUCTURE", "the package is a .csar zip file with no entry listed twice", PACKAGE)
def pk_zip(ctx: PackageContext) -> None:
    """PK-1: the file name ends with .csar (Onboarding refuses a location that does not), it is a zip within Onboarding's zip limits (entry count, size of a file and of all, compression ratio; SEC-15.17), no entry appears twice, no entry path is absolute or climbs out with `..` (Onboarding refuses that for a signed package) and every entry's CRC is good."""
    if not ctx.name.endswith(".csar"):
        raise Fail(f"the file name {ctx.name!r} does not end with .csar (Onboarding refuses a location that does not)")
    module = onboarding()
    try:
        z = module._open_zip(ctx.data)
    except zipfile.BadZipFile:
        raise Fail("not a zip file") from None
    except module.PackageValidationFailed as exc:
        raise Fail(str(exc)) from None
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    if len(set(names)) != len(names):
        raise Fail("an entry is listed twice, so which one a reader sees depends on the reader")
    unsafe = [n for n in names if n.startswith("/") or ".." in n.split("/") or "\\" in n]
    if unsafe:
        raise Fail(f"an entry has an unsafe path ({unsafe[0][:80]!r}): a reader that extracts the package would write outside its directory")
    _verify_crcs(z)


@check("PK-2", "STRUCTURE", "TOSCA-Metadata/TOSCA.meta names an entry definitions file that is in the package", PACKAGE)
def pk_tosca(ctx: PackageContext) -> None:
    """PK-2: TOSCA-Metadata/TOSCA.meta names an `Entry-Definitions:` file that is in the package."""
    _asd_definitions(_open(ctx))


@check("PK-3", "STRUCTURE", "the ASD names the rApp: application_name, application_version and provider are set", PACKAGE)
def pk_identity(ctx: PackageContext) -> None:
    """PK-3 (warning): the ASD sets application_name, application_version and provider; Onboarding accepts a package without them and lists it with placeholder values."""
    z = _open(ctx)
    try:
        _, definitions = _asd_definitions(z)
    except Fail:
        raise Skip("the entry definitions cannot be read (PK-2)") from None
    found = onboarding()._asd_identity(definitions)
    missing = [k for k, field in (("application_name", "name"), ("application_version", "version"), ("provider", "vendor")) if field not in found]
    if missing:
        raise Warn(f"the ASD does not set {', '.join(missing)}: Onboarding accepts that, and lists the package as 'unresolved-until-validated 0.0.0' or without a vendor")


@check("PK-4", "MANIFEST", "manifest.yaml, when present, is valid: runtimeProfiles, limits and operatorUi pass Onboarding's rules", PACKAGE)
def pk_manifest(ctx: PackageContext) -> None:
    """PK-4: manifest.yaml, when present, passes Onboarding's parser (runtimeProfiles including memory quantities, limits, operatorUi).

    The parsed manifest is kept in `ctx.cache["manifest"]` for PK-5. Skips when the package has no manifest.
    """
    z = _open(ctx)
    if "manifest.yaml" not in z.namelist():
        raise Skip("the package has no manifest.yaml (optional)")
    try:
        result = onboarding()._parse_ai_capabilities(_only(z, "manifest.yaml"))
    except onboarding().PARSE_FAILURES as exc:
        raise Fail(f"manifest.yaml: {exc if isinstance(exc, onboarding().PackageValidationFailed) else type(exc).__name__ + ': ' + str(exc)[:200]}") from None
    ctx.cache["manifest"] = result or {}


@check("PK-5", "MANIFEST", "the manifest declares its execution modes and a runtime profile for each, so the platform can size its runtimes", PACKAGE)
def pk_profiles(ctx: PackageContext) -> None:
    """PK-5 (warning): the manifest declares `executionModes` and a runtime profile for each, because without them AIMgF cannot size the runtime."""
    manifest = ctx.cache.get("manifest")
    if manifest is None:
        raise Skip("no valid manifest.yaml (PK-4)")
    modes, profiles = manifest.get("executionModes"), manifest.get("runtimeProfiles")
    problems = []
    if not modes:
        problems.append("executionModes is not declared")
    if not profiles:
        problems.append("runtimeProfiles is not declared: AIMgF cannot size the runtime, and no CPU or memory request or limit reaches the NFO descriptor")
    elif modes:
        problems += [f"{m} is an execution mode with no runtime profile" for m in modes if m not in profiles]
    if problems:
        raise Warn("; ".join(problems))


@check("PK-6", "MANIFEST", "capabilities.yaml, when present, parses and names SDK namespaces", PACKAGE)
def pk_capabilities(ctx: PackageContext) -> None:
    """PK-6: capabilities.yaml, when present, parses (fail); consumes and provides entries are mappings naming one of the six SDK namespaces (warning otherwise, since Onboarding stores them unchecked)."""
    z = _open(ctx)
    if "capabilities.yaml" not in z.namelist():
        raise Skip("the package has no capabilities.yaml (optional)")
    try:
        result = onboarding()._parse_ai_capabilities(_only(z, "capabilities.yaml")) or {}
    except onboarding().PARSE_FAILURES as exc:
        raise Fail(f"capabilities.yaml: {exc if isinstance(exc, onboarding().PackageValidationFailed) else type(exc).__name__ + ': ' + str(exc)[:200]}") from None
    unknown = sorted({str(c.get("namespace")) for key in ("consumes", "provides") for c in result.get(key, []) if isinstance(c, dict)} - SDK_NAMESPACES)
    malformed = [c for key in ("consumes", "provides") for c in result.get(key, []) if not isinstance(c, dict) or "namespace" not in c]
    if malformed:
        raise Warn(f"{len(malformed)} consumes/provides entries are not {{namespace, description}} mappings")
    if unknown:
        raise Warn(f"namespaces {unknown} are not one of the six SDK namespaces ({sorted(SDK_NAMESPACES)}); Onboarding stores them without checking")


@check("PK-7", "MANIFEST", "Files/Sme declarations, when present, are valid JSON", PACKAGE)
def pk_sme(ctx: PackageContext) -> None:
    """PK-7: Files/Sme declarations, when present, are valid under Onboarding's SME parser."""
    z = _open(ctx)
    if not any(n.startswith("Files/Sme/") for n in z.namelist()):
        raise Skip("the package declares no SME providers or service APIs")
    try:
        onboarding()._parse_sme_declarations(z)
    except onboarding().PARSE_FAILURES as exc:
        raise Fail(f"Files/Sme: {type(exc).__name__}: {str(exc)[:200]}") from None


@check("PK-8", "HYGIENE", "the package ships no test suite, cache, environment file or key file", PACKAGE)
def pk_hygiene(ctx: PackageContext) -> None:
    """PK-8 (warning): no cache directory, test suite, .git, node_modules, environment file or key-named file is shipped in the package."""
    z = _open(ctx)
    names = [i.filename for i in z.infolist() if not i.is_dir()]
    stray = [n for n in names if NOT_SHIPPED_PARTS & set(n.split("/")[:-1]) or SECRET_NAMES.search(n)]
    if stray:
        raise Warn(f"{len(stray)} files that should not be shipped, e.g. {', '.join(stray[:3])}")


@check("PK-9", "HYGIENE", "no file in the package holds a private key", PACKAGE)
def pk_no_private_key(ctx: PackageContext) -> None:
    """PK-9: no file up to `MAX_SCANNED_BYTES` holds a PEM private key or the SMO's `ed25519-seed:` key form; a package is distributed, so a key inside it is published."""
    z = _open(ctx)
    holders = [i.filename for i in z.infolist() if not i.is_dir() and i.file_size <= MAX_SCANNED_BYTES and PRIVATE_KEY.search(_scanned(z, i.filename))]
    if holders:
        raise Fail(f"{', '.join(holders[:3])} holds a private key: anyone who gets the package gets the key")


@check("PK-S", "SIGNATURE", "the package is signed by a publisher in the trust store, and nothing was changed after signing", PACKAGE)
def pk_signature(ctx: PackageContext) -> None:
    """PK-S: the package verifies against the trust store, so it is signed by a trusted publisher and no file changed after signing.

    With no trust store: skipped, or a failure under `--require-signed` (Onboarding in that configuration also refuses when it has no trust store). An unsigned package is a
    warning unless signatures are required; every other verification error (tampered, unknown publisher, bad key) is a failure. The publisher is kept in `ctx.cache`.
    """
    if ctx.trust is None:
        if ctx.require_signed:
            raise Fail("signed packages are required but no trust store was given (--trust): this is what Onboarding does with ONBOARDING_REQUIRE_SIGNED_PACKAGES and no ONBOARDING_TRUST_STORE")
        raise Skip("no --trust given: the signature is not checked")
    try:
        verified = csar_signing.verify_csar(ctx.data, ctx.trust)
    except csar_signing.SignatureError as exc:
        # 'Unsigned' is advice (a warning) only when the package carries no signing entry at all; a package with a digest list but a missing signature file is only partly signed, and fails.
        if exc.code == "unsigned" and not ctx.require_signed and not csar_signing.is_signed(_open(ctx).namelist()):
            raise Warn("the package is not signed (accepted by Onboarding unless ONBOARDING_REQUIRE_SIGNED_PACKAGES is on)") from None
        raise Fail(str(exc)) from None
    ctx.cache["publisher"] = verified.publisher


@check("PK-V", "ONBOARDING", "Onboarding's validation accepts the package (the same code POST /packages runs)", PACKAGE)
def pk_onboarding(ctx: PackageContext) -> None:
    """PK-V: Onboarding's own validation accepts the package with the same trust store and policy; the failure message is the reason Onboarding would store as `failureReason`."""
    module = onboarding()
    try:
        entry, artifacts, integrity_hash, identity = module.validate_package_bytes(ctx.data, ctx.name, trust=ctx.trust, require_signed=ctx.require_signed)
    except module.PARSE_FAILURES as exc:
        reason = str(exc) if isinstance(exc, module.PackageValidationFailed) else f"{type(exc).__name__}: {str(exc)[:200]}"
        raise Fail(f"Onboarding would mark this package FAILED: {reason}") from None
    ctx.cache["identity"] = identity
