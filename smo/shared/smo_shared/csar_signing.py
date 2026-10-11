"""Signed rApp packages (PR-RAPP-1): a digest list of every file, a detached ed25519 signature over it, and a trust store of publisher keys.

A CSAR is a zip. A signed one carries two extra entries beside `TOSCA-Metadata/TOSCA.meta`:

    TOSCA-Metadata/DIGESTS.sha256       one line per file, `<sha-256 hex>  <path>`, sorted by path, the two signing entries left out
    TOSCA-Metadata/DIGESTS.sha256.sig   {"version": 1, "algorithm": "ed25519", "keyId": "<fingerprint>", "signature": "<base64>"}
                                        the signature is over the exact bytes of DIGESTS.sha256

`keyId` is the SHA-256 (hex) of the 32 raw bytes of the public key. It only says which key to try: the publisher a package is attributed to is the
name the operator gave that key in the trust store, never anything the package says about itself.

A package is accepted as signed when (1) its key id is in the trust store, (2) the signature verifies under that key, and (3) the digest list
and the package say the same thing: every file is listed with its digest, nothing is listed that is missing, nothing is in the package that is not
listed. A package above the size limits of `ZipLimits` is refused as `too_large` before it is decompressed. Each failure has its own `SignatureError.code`, so a caller (the Onboarding validation, the CLI, the conformance pack) can say what is wrong.

The trust store is a file or a directory of PEM public keys (`load_trust_store`). A file holds one key or several; a directory is read for `*.pub` and
`*.pem` (a Kubernetes ConfigMap mount has dot-entries and symlinks, which are skipped). The publisher name is the file name without its extension
(`acme.pub` -> `acme`; several keys in one file are `acme`, `acme#2`, ...). Public keys are not secrets, so the setting is a path, not a `*_FILE` secret.

ed25519 comes from `cryptography`, which the hashed lock already carries (`pyjwt[crypto]`); signing is deterministic (RFC 8032), so a package built twice
from the same sources with the same key is byte-identical.
"""

import base64
import binascii
import dataclasses
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DIGEST_FILE = "TOSCA-Metadata/DIGESTS.sha256"
SIGNATURE_FILE = "TOSCA-Metadata/DIGESTS.sha256.sig"
SIGNING_FILES = frozenset({DIGEST_FILE, SIGNATURE_FILE})
ALGORITHM = "ed25519"
FORMAT_VERSION = 1
KEY_SUFFIXES = (".pub", ".pem")
_MAX_NAMES_IN_MESSAGE = 5
_DIGEST_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class SignatureError(Exception):
    """The package is not acceptable as a signed package; `str()` says why, `code` says which rule."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TrustStoreError(Exception):
    """The trust store cannot be used (unreadable, no key in it, a key that is not an ed25519 public key). Never carries key material."""


@dataclasses.dataclass(frozen=True)
class Verification:
    """What a successful `verify_csar` / `verify_zip` reports: the trust-store name of the publisher (`publisher`), the key's fingerprint (`key_id`)
    and how many package files the signed digest list covered (`files`, the signing entries not counted).
    """
    publisher: str
    key_id: str
    files: int


@dataclasses.dataclass(frozen=True)
class TrustedKey:
    """One accepted publisher key: the operator's name for it (`publisher`, taken from the key file's name), its fingerprint and the parsed public key.
    """
    publisher: str
    key_id: str
    public_key: Ed25519PublicKey


# ------------------------------------------------------------------------------------------------------------------------------------- keys

def key_id(public_key: Ed25519PublicKey) -> str:
    """The fingerprint of a public key: the SHA-256 (hex) of its 32 raw bytes. It is how a signature entry names the key to try; it is not secret and
    proves nothing by itself.
    """
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def generate_keypair() -> tuple[bytes, bytes]:
    """(private PEM, public PEM) of a new ed25519 key. The private half is unencrypted PKCS#8: keep it in a secret store."""
    private = Ed25519PrivateKey.generate()
    return (private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
            public_pem(private.public_key()))


def public_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


SEED_PREFIX = "ed25519-seed:"


def seed_text(private_key: Ed25519PrivateKey) -> str:
    """The key as one line, `ed25519-seed:<base64 of the 32-byte seed>`: the other form `load_private_key` reads. It is what the committed demo key uses,
    because a file with a PEM private-key header is what secret scanners stop a push for, and a demo key is meant to be public."""
    seed = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return SEED_PREFIX + base64.b64encode(seed).decode("ascii")


def load_private_key(data: bytes) -> Ed25519PrivateKey:
    """An unencrypted PEM (PKCS#8) private key, or the `ed25519-seed:` line of `seed_text` (lines starting with `#` and blank lines are ignored)."""
    lines = [ln.strip() for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if len(lines) == 1 and lines[0].startswith(SEED_PREFIX):
        try:
            return Ed25519PrivateKey.from_private_bytes(base64.b64decode(lines[0][len(SEED_PREFIX):], validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError("not a valid ed25519 seed line") from exc
    pem = data
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("not an unencrypted PEM private key or an ed25519 seed line") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an ed25519 private key")
    return key


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    """Parses one PEM (SubjectPublicKeyInfo) public key. Raises ValueError when the bytes are not a PEM public key or the key is not ed25519."""
    try:
        key = serialization.load_pem_public_key(pem)
    except ValueError as exc:
        raise ValueError("not a PEM public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an ed25519 public key")
    return key


# ------------------------------------------------------------------------------------------------------------------------------ signing

def file_digests(files: Mapping[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items() if name not in SIGNING_FILES}


def digest_list(files: Mapping[str, bytes]) -> bytes:
    """The digest list of a package's files (the signing entries themselves are never in it), as written into the package."""
    digests = file_digests(files)
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(digests.items())).encode("utf-8")


def sign_digests(digests: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """The detached signature entry for a digest list."""
    document = {"version": FORMAT_VERSION, "algorithm": ALGORITHM, "keyId": key_id(private_key.public_key()),
                "signature": base64.b64encode(private_key.sign(digests)).decode("ascii")}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def signing_entries(files: Mapping[str, bytes], private_key: Ed25519PrivateKey | None) -> dict[str, bytes]:
    """The entries to add to a package: the digest list, and with a key its signature. `files` must not hold the signing entries yet."""
    digests = digest_list(files)
    entries = {DIGEST_FILE: digests}
    if private_key is not None:
        entries[SIGNATURE_FILE] = sign_digests(digests, private_key)
    return entries


def sign_csar(data: bytes, private_key: Ed25519PrivateKey, *, timestamp: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0)) -> bytes:
    """A copy of a CSAR with its digest list and signature added (replacing any earlier ones), for a rApp developer who already has a built package.
    The entries are rewritten in the order of the original and the signing entries go last; timestamps are fixed, so the result is repeatable."""
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        names = [i.filename for i in source.infolist() if not i.is_dir()]
        if len(set(names)) != len(names):
            raise SignatureError("malformed", "the package lists a file twice")
        files = {name: source.read(name) for name in names if name not in SIGNING_FILES}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in {**files, **signing_entries(files, private_key)}.items():
            info = zipfile.ZipInfo(name, timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, content)
    return out.getvalue()


# ----------------------------------------------------------------------------------------------------------------------------- trust store

class TrustStore:
    """The set of publisher keys a package may be signed with, looked up by key id. Built by `load_trust_store`.

    Truthy when it holds at least one key. When two entries share a key id the later one wins (the keys are held in a dict by id), so the same key
    listed under two names is reported under the second name.
    """
    def __init__(self, keys: list[TrustedKey]):
        self._by_id = {k.key_id: k for k in keys}
        self.keys = list(self._by_id.values())

    def __bool__(self) -> bool:
        return bool(self._by_id)

    def get(self, key_id_: str) -> TrustedKey | None:
        return self._by_id.get(key_id_)


_PEM_BLOCK = re.compile(rb"-----BEGIN PUBLIC KEY-----.+?-----END PUBLIC KEY-----", re.S)


def _keys_of(path: Path) -> list[TrustedKey]:
    """The keys in one trust-store file, one per `PUBLIC KEY` PEM block, named after the file (`acme`, then `acme#2`, `acme#3` ...).

    Raises TrustStoreError when the file cannot be read, holds no PEM block, or a block is not an ed25519 public key. The message carries the file
    name and the reason, never key material.
    """
    try:
        text = path.read_bytes()
    except OSError as exc:
        raise TrustStoreError(f"trust store file {path.name} cannot be read: {exc.strerror or type(exc).__name__}") from exc
    blocks = _PEM_BLOCK.findall(text)
    if not blocks:
        raise TrustStoreError(f"trust store file {path.name} holds no PEM public key")
    keys = []
    for n, block in enumerate(blocks, start=1):
        try:
            public = load_public_key(block)
        except ValueError as exc:
            raise TrustStoreError(f"trust store file {path.name}: {exc}") from exc
        keys.append(TrustedKey(path.stem if n == 1 else f"{path.stem}#{n}", key_id(public), public))
    return keys


def load_trust_store(location: str | Path) -> TrustStore:
    """The accepted publisher keys at `location` (a file, or a directory of `*.pub` / `*.pem`). Raises TrustStoreError when it is missing, unreadable,
    holds no key, or a file in it is not an ed25519 public key: a store that quietly trusts fewer keys than the operator listed is worse than a refusal."""
    path = Path(location)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if not p.name.startswith(".") and p.suffix in KEY_SUFFIXES and p.is_file())
    elif path.is_file():
        files = [path]
    else:
        raise TrustStoreError(f"trust store {path.name or str(path)} does not exist")
    keys = [key for f in files for key in _keys_of(f)]
    if not keys:
        raise TrustStoreError(f"trust store {path.name or str(path)} holds no public key (*.pub or *.pem)")
    return TrustStore(keys)


# ---------------------------------------------------------------------------------------------------------------------------- verification

def _names(names: list[str]) -> str:
    shown = ", ".join(sorted(names)[:_MAX_NAMES_IN_MESSAGE])
    return shown + (f" and {len(names) - _MAX_NAMES_IN_MESSAGE} more" if len(names) > _MAX_NAMES_IN_MESSAGE else "")


def is_signed(names: list[str]) -> bool:
    """Whether the package carries any signing entry at all (an unsigned package has neither)."""
    return bool(SIGNING_FILES & set(names))


def _parse_digests(raw: bytes) -> dict[str, str]:
    """Parses a `DIGESTS.sha256` into {path: sha-256 hex}. Raises SignatureError("malformed") for non-UTF-8 text, a line that is not `<64 hex>
    <path>`, or a path listed twice (a second digest for a path must not silently replace the first).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SignatureError("malformed", "the digest list is not UTF-8 text") from exc
    digests: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _DIGEST_LINE.match(line)
        if not match:
            raise SignatureError("malformed", f"the digest list is malformed at line {number}")
        digest, name = match.groups()
        if name in digests:
            raise SignatureError("malformed", f"the digest list names {name} twice")
        digests[name] = digest
    return digests


def _signature_document(raw: bytes) -> tuple[str, bytes]:
    """Reads the `.sig` entry and returns (key id, raw signature bytes). Raises SignatureError("malformed") when it is not JSON, is not a version 1
    ed25519 document, lacks `keyId` or `signature`, or the signature is not strict base64. Nothing here checks the signature itself.
    """
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignatureError("malformed", "the signature entry is not JSON") from exc
    if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION or document.get("algorithm") != ALGORITHM:
        raise SignatureError("malformed", f"the signature entry is not a version {FORMAT_VERSION} {ALGORITHM} signature")
    key, signature = document.get("keyId"), document.get("signature")
    if not isinstance(key, str) or not isinstance(signature, str):
        raise SignatureError("malformed", "the signature entry has no keyId or signature")
    try:
        return key, base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("malformed", "the signature is not base64") from exc


@dataclasses.dataclass(frozen=True)
class ZipLimits:
    """How much archive `verify_zip` is willing to read before it has trusted anything (a package arrives from outside, and the digests are checked
    only after every file has been decompressed, so an unbounded read is a zip bomb).

    `max_entries` counts every entry of the central directory, directories included. `max_member_bytes` and `max_total_bytes` bound the uncompressed
    size of one file and of all files together. `max_ratio` bounds uncompressed size over compressed size of one file, and applies only to a file above
    `ratio_floor_bytes` (a few hundred zero bytes legitimately compress a hundredfold). `max_signing_bytes` bounds the digest list and the signature entry.
    The defaults are far above any real rApp package (the committed samples are under 50 kB) and far below what exhausts a worker's memory.
    """
    max_entries: int = 2000
    max_member_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    max_ratio: int = 200
    ratio_floor_bytes: int = 1024 * 1024
    max_signing_bytes: int = 2 * 1024 * 1024


DEFAULT_LIMITS = ZipLimits()


def check_limits(archive: zipfile.ZipFile, limits: ZipLimits) -> None:
    """Refuses (`SignatureError("too_large")`) an archive whose central directory already shows it is too big: too many entries, a file or the total above
    the limit, or a file with a compression ratio above `max_ratio`. Reads only the directory (the declared sizes), never the data.

    Public because every reader of a package's bytes must apply it before it decompresses anything, not only the signature check: Onboarding's parse of an unsigned package
    does (SEC-15.17, `onboarding/app/package_validation.py`).
    """
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise SignatureError("too_large", f"the package has {len(infos)} entries, more than the {limits.max_entries} allowed")
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        cap = limits.max_signing_bytes if info.filename in SIGNING_FILES else limits.max_member_bytes
        if info.file_size > cap:
            raise SignatureError("too_large", f"the package file {info.filename[:80]!r} is {info.file_size} bytes uncompressed, more than the {cap} allowed")
        # the declared compressed size is zero only for an empty file; max(.., 1) keeps the ratio defined without letting a lie through
        if info.file_size > limits.ratio_floor_bytes and info.file_size > limits.max_ratio * max(info.compress_size, 1):
            raise SignatureError("too_large", f"the package file {info.filename[:80]!r} compresses more than {limits.max_ratio} to 1 (a zip bomb?)")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise SignatureError("too_large", f"the package is more than {limits.max_total_bytes} bytes uncompressed")


def read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Reads one file of the archive and refuses (`too_large`) one that really holds more than `limit` bytes. The directory's declared size can lie, so the
    read itself is bounded: at most `limit + 1` bytes are ever decompressed. Public for the same reason as `check_limits`."""
    with archive.open(name) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise SignatureError("too_large", f"the package file {name[:80]!r} is larger than the {limit} bytes allowed")
    return data


def verify_zip(archive: zipfile.ZipFile, trust: TrustStore, limits: ZipLimits = DEFAULT_LIMITS) -> Verification:
    """Check a package against the trust store. Raises SignatureError (`code`: too_large, unsigned, malformed, unknown_publisher, bad_signature, modified,
    missing, added). Returns who signed it when everything agrees.

    The size limits of `limits` are checked first, from the zip directory, and again while reading (a directory can understate a file), so a zip bomb is
    refused with `too_large` before it is decompressed in full.
    """
    check_limits(archive, limits)
    entries = [i.filename for i in archive.infolist() if not i.is_dir()]
    if len(set(entries)) != len(entries):
        raise SignatureError("malformed", "the package lists a file twice")        # which of two entries a reader sees must not depend on the reader
    for name in entries:
        if name.startswith("/") or ".." in name.split("/") or "\\" in name:
            raise SignatureError("malformed", f"the package has a file with an unsafe path: {name[:80]!r}")
    has_digests, has_signature = DIGEST_FILE in entries, SIGNATURE_FILE in entries
    if not has_digests and not has_signature:
        raise SignatureError("unsigned", "the package is not signed (no digest list and no signature)")
    if not has_signature:
        raise SignatureError("unsigned", f"the package has a digest list but no signature ({SIGNATURE_FILE} is missing)")
    if not has_digests:
        raise SignatureError("malformed", f"the package has a signature but no digest list ({DIGEST_FILE} is missing)")
    digest_bytes = read_member(archive, DIGEST_FILE, limits.max_signing_bytes)
    key_name, signature = _signature_document(read_member(archive, SIGNATURE_FILE, limits.max_signing_bytes))
    trusted = trust.get(key_name)
    if trusted is None:
        raise SignatureError("unknown_publisher", f"unknown publisher: the package is signed with key {key_name[:16]}..., which is not in the trust store")
    try:
        trusted.public_key.verify(signature, digest_bytes)
    except InvalidSignature as exc:
        raise SignatureError("bad_signature", f"the signature does not verify under the key of publisher {trusted.publisher} "
                                              "(the digest list was altered, or it was signed with another key)") from exc
    listed = _parse_digests(digest_bytes)
    present = [n for n in entries if n not in SIGNING_FILES]
    missing = [n for n in listed if n not in present]
    if missing:
        raise SignatureError("missing", f"file removed from the package: {_names(missing)} is in the signed digest list but not in the package")
    added = [n for n in present if n not in listed]
    if added:
        raise SignatureError("added", f"file added to the package: {_names(added)} is not covered by the signed digest list")
    changed = [n for n in present if hashlib.sha256(read_member(archive, n, limits.max_member_bytes)).hexdigest() != listed[n]]
    if changed:
        raise SignatureError("modified", f"file modified after signing: {_names(changed)} does not match its signed digest")
    return Verification(trusted.publisher, trusted.key_id, len(present))


def verify_csar(data: bytes, trust: TrustStore) -> Verification:
    """Checks a whole CSAR (the zip bytes) against the trust store and returns who signed it; see `verify_zip` for the checks and the error codes.

    A file that is not a zip raises SignatureError("malformed").
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return verify_zip(archive, trust)
    except zipfile.BadZipFile as exc:
        raise SignatureError("malformed", "the package is not a zip file") from exc
