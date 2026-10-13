"""Package validation, the part of Onboarding that needs neither the database nor the web framework.

`validate_package_bytes` is what `POST /packages` runs on a fetched CSAR (`app/main.py` fetches it, then calls this) and what the offline validator of
the rApp conformance pack runs on a file (`conformance/rapp`, PR-RAPP-3.1), so a developer sees the verdict Onboarding will give. It raises
`PackageValidationFailed` (with the reason) or one of the parse errors in `PARSE_FAILURES`; the caller treats all of them as "this package fails".

PR-RAPP-1: when a trust store is given the package signature is checked (smo_shared/csar_signing.py), and `require_signed` refuses a package that has none.

SEC-15.17: a package comes from outside, so every place that opens its zip applies the size limits `csar_signing.verify_zip` applies (`ZIP_LIMITS`: entry count, size of one
file and of all, compression ratio) before it decompresses anything, whether or not the package is signed, and every read of a file is bounded; a package over them fails
with `PackageValidationFailed` like any other bad package. Edit with that in mind: a new `ZipFile(...)` or `z.read(...)` in this file goes through `_open_zip` and `_read`.
"""

import hashlib
import json
import re
import zipfile
from io import BytesIO
from typing import Any

import yaml

from smo_shared import csar_signing
from smo_shared.operator_ui import OperatorUiInvalid, validate_operator_ui
from smo_shared.runtime_resources import is_quantity
from smo_shared.webhook import normalise_base_url


class PackageValidationFailed(Exception):
    """A package fails validation; the message says why and is shown to the operator as `failureReason`.

    Raised for the reference validator chain's checks (rapp-manager-models' csar/validator/*): a location without the `.csar` name (NamingValidator), a duplicate of an
    already onboarded package (AsdDescriptorValidator's uniqueness check, adapted to this build's identity, a content hash; HISTORY.md §5), and every rule of the
    manifest, capabilities, limits, runtime-profile, operator-UI and signature checks. It is a member of PARSE_FAILURES.
    """



# The limits applied to every package zip before and while it is read: the ones `csar_signing.verify_zip` uses, so a signed and an unsigned package are held to the same rule.
# A name here, not a literal at each use, so a test can lower them.
ZIP_LIMITS = csar_signing.DEFAULT_LIMITS


def _open_zip(data: bytes) -> zipfile.ZipFile:
    """Opens the package bytes as a zip after checking the directory against `ZIP_LIMITS`; the caller closes it.

    Raises zipfile.BadZipFile for bytes that are not a zip and PackageValidationFailed for a package over the limits (too many entries, a file or the whole too big, or a
    ratio that looks like a zip bomb). Nothing is decompressed here: only the central directory is read.
    """
    archive = zipfile.ZipFile(BytesIO(data))
    try:
        csar_signing.check_limits(archive, ZIP_LIMITS)
    except csar_signing.SignatureError as exc:
        archive.close()
        raise PackageValidationFailed(f"package too large to read: {exc}") from exc
    return archive


def _read(z: zipfile.ZipFile, name: str) -> bytes:
    """Returns the bytes of one file of the package, refusing (`PackageValidationFailed`) a file that really holds more than `ZIP_LIMITS.max_member_bytes`.

    The directory's declared size can understate a file, so the read is bounded as well as the directory checked (`_open_zip`); raises KeyError for a missing file, as `ZipFile.read` does.
    """
    try:
        return csar_signing.read_member(z, name, ZIP_LIMITS.max_member_bytes)
    except csar_signing.SignatureError as exc:
        raise PackageValidationFailed(f"package too large to read: {exc}") from exc


# What counts as "this package fails to validate" besides PackageValidationFailed itself: a zip that is not one, a missing entry, malformed YAML or JSON in a
# package file. (Onboarding adds the location being unreachable and NFO refusing the descriptor.)
PARSE_FAILURES = (zipfile.BadZipFile, KeyError, FileNotFoundError, PackageValidationFailed, yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError)


# ASD property name -> key in the identity dict that `onboard_package` copies onto the package row. The first three are the package's name, version and vendor.
_ASD_IDENTITY_FIELDS = {
    "application_name": "name", "application_version": "version", "provider": "vendor",
    # The four descriptor fields of the real ASD schema (asd_types.yaml's tosca.nodes.asd node type, checked against nonrtric-plt-rappmanager's sample
    # CSARs). They are stored as given; package identity and uniqueness stay on integrity_hash.
    "descriptor_id": "descriptor_id", "descriptor_invariant_id": "descriptor_invariant_id",
    "descriptor_version": "descriptor_version", "schema_version": "schema_version",
}


def _asd_identity(definitions: str) -> dict[str, str]:
    """Reads the ASD's identity properties out of the entry definitions text and returns them as {name, version, vendor, descriptor_id, ...}; a property that is absent or empty is left out.

    The properties are those of `_ASD_IDENTITY_FIELDS` (application_name, application_version, provider and the four descriptor fields of the real ASD schema,
    `tosca.nodes.asd`). It is a line scan, not a YAML parse: they are flat scalars, and this module does not parse the Definitions file as YAML. The first occurrence of a
    key wins; a trailing ` # comment` and surrounding quotes are stripped.
    """
    found: dict[str, str] = {}
    for line in definitions.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in _ASD_IDENTITY_FIELDS and _ASD_IDENTITY_FIELDS[key] not in found:
            # A YAML comment starts at ' #'; a '#' inside a value (no space before it) is kept.
            value = value.split(" #", 1)[0].strip().strip("\"'")
            if value:
                found[_ASD_IDENTITY_FIELDS[key]] = value
    return found


def _parse_ai_capabilities(z: zipfile.ZipFile) -> dict | None:
    """Reads the optional AI Platform declarations from `manifest.yaml` and `capabilities.yaml` at the CSAR root; returns a dict, or None when neither file gives anything.

    `manifest.yaml`: `rappManifest.manifestVersion` and `aiRuntimeSdkVersion`; the AI-runtime keys `executionModes`, `autonomyModes`, `requiredServices` and `runtimeProfiles`
    (under `rappManifest` or at the top level, W7-03); `limits` (AI-10.1); `operatorApi.baseUrl` (call flow 01b); `operatorUi` (GUI-8.2). `capabilities.yaml`: `capabilities.consumes` and `provides`, each a list of
    `{namespace, description}` naming which of the SDK's six namespaces (data, analytics, models, lifecycle, intent, platform) the rApp uses. The layout is specified in
    docs/RAPP_PACKAGING.md.

    Both files are optional and independent; a package with neither returns None and `ApplicationPackage.ai_capabilities` stays NULL. Unlike `_asd_identity` these files
    are nested, so they are parsed with `yaml.safe_load`. Raises PackageValidationFailed for a file or section that is not a mapping, a bad runtime profile, limit or operator
    page; yaml.YAMLError for malformed YAML (both are in PARSE_FAILURES).
    """
    result: dict = {}
    names = z.namelist()
    if "manifest.yaml" in names:
        manifest = yaml.safe_load(_read(z, "manifest.yaml")) or {}
        if not isinstance(manifest, dict):
            raise PackageValidationFailed("manifest.yaml must be a mapping")
        rapp_manifest = manifest.get("rappManifest") or {}
        if not isinstance(rapp_manifest, dict):
            raise PackageValidationFailed("manifest.yaml: rappManifest must be a mapping")
        result["manifestVersion"] = rapp_manifest.get("manifestVersion")
        result["aiRuntimeSdkVersion"] = rapp_manifest.get("aiRuntimeSdkVersion")
        # Wave 7 (HISTORY.md W7-03): the AI-runtime part of
        # the manifest — which execution modes the package supports and the
        # compute each one needs. Accepted either under `rappManifest` or at
        # the manifest's top level (the SMO_Wave_10 package layout).
        for key in ("executionModes", "autonomyModes", "requiredServices", "runtimeProfiles"):
            value = rapp_manifest.get(key, manifest.get(key))
            if value is not None:
                result[key] = value
        profiles = result.get("runtimeProfiles")
        if profiles is not None:
            result["runtimeProfiles"] = _validate_runtime_profiles(profiles, result.get("executionModes"))
        # AI-10.1: what this rApp may do on the network, declared by the package and enforced by the platform (`limits.configJobsPerHour`).
        limits = rapp_manifest.get("limits", manifest.get("limits"))
        if limits is not None:
            result["limits"] = _validate_limits(limits)
        # Call flow 01b (DOC-CF-1.1 review): where the rApp's operator API is reached, so CreateInstance needs no address typed by the operator (who may still
        # give one, which wins). `operatorApi.baseUrl` passes the same guard as an operator-given `operatorApiBase` (smo_shared.webhook.normalise_base_url).
        operator_api = rapp_manifest.get("operatorApi", manifest.get("operatorApi"))
        if operator_api is not None:
            result["operatorApi"] = _validate_operator_api(operator_api)
        # GUI-8.2: the operator page the rApp declares (docs/adr/0004-operator-ui-declaration.md). Optional; absent -> the key is absent.
        operator_ui = rapp_manifest.get("operatorUi", manifest.get("operatorUi"))
        if operator_ui is not None:
            try:
                result["operatorUi"] = validate_operator_ui(operator_ui)
            except OperatorUiInvalid as exc:
                raise PackageValidationFailed(str(exc)) from exc
    if "capabilities.yaml" in names:
        parsed = yaml.safe_load(_read(z, "capabilities.yaml")) or {}
        if not isinstance(parsed, dict):
            raise PackageValidationFailed("capabilities.yaml must be a mapping")
        caps = parsed.get("capabilities") or {}
        if not isinstance(caps, dict):
            raise PackageValidationFailed("capabilities.yaml: capabilities must be a mapping")
        result["consumes"] = caps.get("consumes") or []
        result["provides"] = caps.get("provides") or []
    return result or None


# The execution modes a runtime profile may be declared for.
EXECUTION_MODES = ("TRAINING", "VALIDATION", "EMULATION", "INFERENCE")


def _validate_runtime_profiles(profiles, execution_modes) -> dict:
    """W7-03: `runtimeProfiles` maps an execution mode (TRAINING /
    VALIDATION / EMULATION / INFERENCE) to {cpu, memory, gpu}. A profile
    for a mode the manifest doesn't declare in `executionModes` (when it
    declares any) is a packaging error, as is an unknown mode or a
    non-numeric cpu/gpu. Raises PackageValidationFailed -> the package fails onboarding (FAILED).
    """
    if not isinstance(profiles, dict):
        raise PackageValidationFailed("runtimeProfiles must be a mapping of execution mode -> profile")
    out = {}
    for mode, profile in profiles.items():
        if mode not in EXECUTION_MODES:
            raise PackageValidationFailed(f"runtimeProfiles: unknown execution mode {mode!r}")
        if execution_modes and mode not in execution_modes:
            raise PackageValidationFailed(f"runtimeProfiles: {mode} is not one of the declared executionModes")
        if not isinstance(profile, dict):
            raise PackageValidationFailed(f"runtimeProfiles.{mode} must be a mapping")
        clean = {}
        for field in ("cpu", "gpu"):
            if field in profile:
                # A bool would pass the number test (bool is a subclass of int), so it is refused explicitly.
                if not isinstance(profile[field], (int, float)) or isinstance(profile[field], bool) or profile[field] < 0:
                    raise PackageValidationFailed(f"runtimeProfiles.{mode}.{field} must be a non-negative number")
                clean[field] = profile[field]
        if "memory" in profile:
            clean["memory"] = str(profile["memory"])
            # PR-RAPP-2.1: the memory becomes a container limit in the NFO descriptor, so it must be a Kubernetes quantity (16Gi, 512Mi, 4G); anything else
            # (`16 GB`, a bool) would be an invalid pod spec on the day a deployment manager applies it
            if isinstance(profile["memory"], bool) or not is_quantity(clean["memory"]):
                raise PackageValidationFailed(f"runtimeProfiles.{mode}.memory must be a Kubernetes quantity such as 4Gi, 512Mi or 4G, not {clean['memory'][:40]!r}")
        out[mode] = clean
    return out


# name -> (whole numbers only?, largest value). Every limit is positive. Enforced by RAN NF OAM (AI-10.2, AI-10.3).
LIMIT_SPECS = {"configJobsPerHour": (True, 100_000), "maxElementsPerJob": (True, 10_000), "maxChangePercent": (False, 10_000)}


def _validate_operator_api(operator_api) -> dict:
    """`operatorApi` is a mapping with one key, `baseUrl`: an http(s) base URL with no credentials, query or fragment and not a loopback, link-local or
    metadata address (`normalise_base_url`, the rule an operator-given `operatorApiBase` follows). Returns `{"baseUrl": <normalised>}`; anything else raises
    PackageValidationFailed naming the rule."""
    if not isinstance(operator_api, dict) or set(operator_api) != {"baseUrl"}:
        raise PackageValidationFailed("operatorApi must be a mapping with exactly one key, baseUrl")
    base = normalise_base_url(operator_api["baseUrl"]) if isinstance(operator_api["baseUrl"], str) else None
    if base is None:
        raise PackageValidationFailed("operatorApi.baseUrl must be an http or https URL without credentials, query or fragment, "
                                      "and not a loopback, link-local or metadata address")
    return {"baseUrl": base}


def _validate_limits(limits) -> dict:
    """AI-10.1/10.3: `limits` maps a limit name to a positive number. An unknown name, a value that is not a number (a bool is not one), one out of
    range, or a fraction where a whole number is needed is a packaging error (the package fails onboarding): a limit the platform cannot enforce
    must not be silently ignored. `configJobsPerHour`: how many CM write jobs the rApp may start in any hour; `maxElementsPerJob`: how many managed
    elements one job may touch (blast radius); `maxChangePercent`: how far, in percent of its current value, a numeric value may move in one write
    (magnitude)."""
    if not isinstance(limits, dict):
        raise PackageValidationFailed("limits must be a mapping of limit name -> number")
    out = {}
    for name, value in limits.items():
        if name not in LIMIT_SPECS:
            raise PackageValidationFailed(f"limits: unknown limit {name!r} (known: {', '.join(sorted(LIMIT_SPECS))})")
        whole, largest = LIMIT_SPECS[name]
        number = isinstance(value, int) if whole else isinstance(value, (int, float))
        # A bool is an int in Python and must not count as a limit. `value != value` is true only for NaN, which the range test already refuses; it is a second, explicit check.
        if not number or isinstance(value, bool) or not 0 < value <= largest or value != value:
            raise PackageValidationFailed(f"limits.{name} must be a {'whole number' if whole else 'number'} above 0 and at most {largest}")
        out[name] = value
    return out


def _parse_sme_declarations(z: zipfile.ZipFile) -> dict | None:
    """Reads the SME declarations out of the CSAR (`Files/Sme/providers/*.json`, `Files/Sme/serviceapis/*.json`) and returns {providers, serviceApis}, or None when there are none.

    The layout is that of the O-RAN SC rApp Manager's sample packages (`nonrtric-plt-rappmanager/sample-rapp-generator`): CAPIF `APIProviderEnrolmentDetails` and
    `ServiceAPIDescription` documents. They are stored raw at onboarding and registered with SME per instance by rApp Management's bootstrap-complete (the reference registers
    at instance deployment, not at package priming). Optional and additive like `manifest.yaml`: a package without the directories gives None, `sme_declarations` stays
    NULL and there is nothing to register. File names are sorted so the order is deterministic. Raises json.JSONDecodeError or UnicodeDecodeError for a file that is not
    valid UTF-8 JSON (both are in PARSE_FAILURES).
    """
    names = z.namelist()
    providers = [json.loads(_read(z, n)) for n in sorted(names) if n.startswith("Files/Sme/providers/") and n.endswith(".json")]
    service_apis = [json.loads(_read(z, n)) for n in sorted(names) if n.startswith("Files/Sme/serviceapis/") and n.endswith(".json")]
    if not providers and not service_apis:
        return None
    return {"providers": providers, "serviceApis": service_apis}



def verify_signature(data: bytes, trust: "csar_signing.TrustStore | None", require_signed: bool) -> dict[str, Any]:
    """PR-RAPP-1.4/1.5. Returns what is known about the signature: {} when nothing was asked for (no trust store, signing not required: the package is taken
    as before), else `{"signature_verified": True, "signed_by": publisher}`. Raises PackageValidationFailed with a clear reason when:
      - signing is required and there is no trust store (a misconfiguration, so every package is refused rather than every package accepted);
      - the package carries a signature or digest list and it does not verify (tampered, added or removed file, wrong key, unknown publisher);
      - signing is required and the package has none.
    With a trust store and `require_signed` off, an unsigned package is accepted but not marked verified."""
    if trust is None:
        if require_signed:
            raise PackageValidationFailed("signed packages are required (ONBOARDING_REQUIRE_SIGNED_PACKAGES) but no trusted publisher keys are configured (ONBOARDING_TRUST_STORE)")
        return {}
    if not require_signed and not _carries_signing_entries(data):
        return {"signature_verified": False}
    try:
        verification = csar_signing.verify_csar(data, trust)
    except csar_signing.SignatureError as exc:
        raise PackageValidationFailed(f"package signature: {exc}") from exc
    return {"signature_verified": True, "signed_by": verification.publisher}


def _carries_signing_entries(data: bytes) -> bool:
    """True when the zip has a digest list or a signature entry; false for an unsigned package and for bytes that are not a zip (those fail later, in the parse).
    """
    try:
        with _open_zip(data) as z:
            return csar_signing.is_signed(z.namelist())
    except zipfile.BadZipFile:
        return False


def validate_package_bytes(data: bytes, location: str, *, trust: "csar_signing.TrustStore | None" = None,
                           require_signed: bool = False) -> tuple[str, list[tuple[str, str]], str, dict]:
    """Open TOSCA-Metadata/Definitions/Artifacts of a fetched CSAR, per Onboarding LLD section 1: (entry definitions, artifacts, integrity hash, identity).
    `location` gives each artifact its access URL and must end in `.csar` (NamingValidator). The signature is checked first (a tampered package is not parsed); see verify_signature."""
    # Order matters: the signature is verified before the zip is opened for parsing (a tampered package is never parsed). The integrity hash is the SHA-256 of the whole
    # fetched byte string, so it identifies the package file, not its contents.
    if not location.endswith(".csar"):
        raise PackageValidationFailed(f"package location {location!r} does not end with .csar")
    signature = verify_signature(data, trust, require_signed)
    with _open_zip(data) as z:
        meta = _read(z, "TOSCA-Metadata/TOSCA.meta").decode()
        # TOSCA.meta is read as plain 'Key: value' lines; only Entry-Definitions is needed, and the path after the colon is looked up in the zip.
        entry_line = next((l for l in meta.splitlines() if l.startswith("Entry-Definitions:")), None)
        if entry_line is None:       # a bare next() raised StopIteration here, which no caller catches
            raise PackageValidationFailed("TOSCA-Metadata/TOSCA.meta has no Entry-Definitions: line")
        entry_definitions = entry_line.split(":", 1)[1].strip()
        identity: dict[str, Any] = _asd_identity(_read(z, entry_definitions).decode(errors="replace"))  # raises KeyError if missing/malformed
        # Every file under Artifacts/ is registered (directories are skipped) with an access URL that points into the package at its location.
        artifacts = [(n, f"{location}#{n}") for n in z.namelist() if n.startswith("Artifacts/") and not n.endswith("/")]
        ai_capabilities = _parse_ai_capabilities(z)
        if ai_capabilities is not None:
            identity["ai_capabilities"] = ai_capabilities
        sme_declarations = _parse_sme_declarations(z)
        if sme_declarations is not None:
            identity["sme_declarations"] = sme_declarations
    # Merges what verify_signature knows (signature_verified, signed_by) into the identity; when it returned {} (nothing asked for) the keys are absent and `onboard_package` defaults signature_verified to True.
    identity.update(signature)
    integrity_hash = hashlib.sha256(data).hexdigest()
    return entry_definitions, artifacts, integrity_hash, identity
