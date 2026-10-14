"""Encryption at rest of the secrets in the shared `module_identity` table (SEC-15.18).

The table keeps the SME invoker identity of every module and of every rApp instance (`module_identity.py`). Its secret column was the one place a
copy of the database (a backup, a read replica, a dump sent for support) held credentials in clear. With a key configured it holds this instead:

    enc:v1:<key id>:<base64url(nonce || AES-256-GCM ciphertext and tag)>

`GUI_TOTP_KEY` (gui-bff/app/totp.py) is the model and the primitive is the same: AES-256-GCM from `cryptography` (a locked dependency of the image),
the key derived with HKDF-SHA256 from a configured string of at least 32 characters (`openssl rand -base64 32`), a fresh random 12-byte nonce per
write. The module name (the row's primary key) is the associated data, so a ciphertext copied into another row does not decrypt. The key id
(first 4 bytes, hex, of SHA-256 over the derived key, which does not reveal the key) says which key sealed a value, so a rotation can read old
values while writing new ones.

Configuration, read once when a store is made (a mounted secret changes only with a restart, as for every other `*_FILE` secret):

    SMO_IDENTITY_KEY / SMO_IDENTITY_KEY_FILE                    the key values are written with
    SMO_IDENTITY_PREVIOUS_KEY / SMO_IDENTITY_PREVIOUS_KEY_FILE  the key of the rotation before: values sealed with it are still read

Every replica of every module must have the same key, or one replica cannot read what another wrote.

What is accepted, so that an upgrade never locks an instance out:

  no key, plaintext value         read as before; written as before (plaintext).
  key set, plaintext value        read, with a warning and `smo_identity_unencrypted_reads_total`: the row has not been encrypted yet
                                  (`python -m smo_shared.module_identity encrypt-existing`). Never refused.
  key set, sealed with it         read.
  key set, sealed with previous   read, with the same warning and counter kind: `encrypt-existing` re-seals it with the current key.
  no key, sealed value            IdentityKeyError naming SMO_IDENTITY_KEY: ciphertext is never taken for a secret.
  sealed with an unknown key, or  IdentityKeyError naming the variable. A wrong key and a changed value cannot be told apart, and are not.
  a value that does not verify

An error message names the variable and never a key, a ciphertext or a secret. `cryptography` is imported only when a key is configured or a sealed
value is met, so a deployment that never turns this on needs nothing new.
"""

import base64
import hashlib
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field

from .metrics import record_identity_unencrypted_read
from .secretfile import SecretConflict, SecretFileError, read_secret

log = logging.getLogger("smo.identity_secrets")

PREFIX = "enc:v1:"
KEY_VARIABLE = "SMO_IDENTITY_KEY"
PREVIOUS_KEY_VARIABLE = "SMO_IDENTITY_PREVIOUS_KEY"
MIN_KEY_LENGTH = 32          # as GUI_TOTP_KEY
_NONCE_BYTES = 12


class IdentityKeyError(RuntimeError):
    """The key configuration is unusable, or a stored secret cannot be read with it. The message names the variable, never a key or a value."""


@dataclass(frozen=True)
class _Key:
    kid: str
    material: bytes


@dataclass(frozen=True)
class IdentityKeys:
    """The keys in force: `current` (None: nothing is encrypted) and, during a rotation, `previous`."""
    current: _Key | None = None
    previous: _Key | None = None
    _by_kid: Mapping[str, _Key] = field(default_factory=dict, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"IdentityKeys(current={self.current.kid if self.current else None}, previous={self.previous.kid if self.previous else None})"


def _derive(raw: str) -> _Key:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"smo-module-identity/v1").derive(raw.encode())
    return _Key(kid=hashlib.sha256(material).digest()[:4].hex(), material=material)


def _read(name: str, environ: Mapping[str, str]) -> str | None:
    try:
        value = read_secret(name, environ)
    except (SecretConflict, SecretFileError) as exc:
        raise IdentityKeyError(str(exc)) from exc            # names the variable and path, never the contents
    if value is not None and len(value) < MIN_KEY_LENGTH:
        raise IdentityKeyError(f"{name} must be at least {MIN_KEY_LENGTH} characters (for example `openssl rand -base64 32`)")
    return value


def load_keys(environ: Mapping[str, str] = os.environ) -> IdentityKeys:
    """The keys the environment configures. No key at all is a valid answer (nothing is encrypted); an unusable configuration raises IdentityKeyError:
    both `NAME` and `NAME_FILE` set, an unreadable file, a key shorter than 32 characters, a previous key without a current one or equal to it.
    """
    current_raw, previous_raw = _read(KEY_VARIABLE, environ), _read(PREVIOUS_KEY_VARIABLE, environ)
    if previous_raw is not None and current_raw is None:
        raise IdentityKeyError(f"{PREVIOUS_KEY_VARIABLE} is set but {KEY_VARIABLE} is not: a rotation needs the new key as well")
    if current_raw is None:
        return IdentityKeys()
    if previous_raw == current_raw:
        raise IdentityKeyError(f"{PREVIOUS_KEY_VARIABLE} is the same key as {KEY_VARIABLE}: remove it")
    current = _derive(current_raw)
    previous = _derive(previous_raw) if previous_raw is not None else None
    return IdentityKeys(current, previous, {k.kid: k for k in (previous, current) if k is not None})


def is_sealed(stored: str) -> bool:
    """True when `stored` is in the encrypted format (it starts with `enc:v1:`)."""
    return stored.startswith(PREFIX)


def _aad(module: str) -> bytes:
    return b"smo-module-identity/v1/" + module.encode()


def seal(keys: IdentityKeys, module: str, secret: str) -> str:
    """`secret` as it is stored for `module`: the envelope under the current key, or `secret` unchanged when no key is configured."""
    if keys.current is None:
        return secret
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(keys.current.material).encrypt(nonce, secret.encode(), _aad(module))
    return f"{PREFIX}{keys.current.kid}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"


def key_of(stored: str) -> str | None:
    """The key id a sealed value names, or None for a plaintext value or a malformed envelope."""
    if not is_sealed(stored):
        return None
    kid, _, body = stored[len(PREFIX):].partition(":")
    return kid if kid and body else None


def unseal(keys: IdentityKeys, module: str, stored: str, *, quiet: bool = False) -> str:
    """The secret in `stored` for `module`. A plaintext value is returned as it is (warning and counter when a key is configured: the row is not
    encrypted yet). Raises IdentityKeyError for a sealed value that cannot be read: no key configured, a key this process does not have, or a value
    that does not verify (wrong key, changed value, or a value copied from another row).
    """
    if not is_sealed(stored):
        if keys.current is not None and not quiet:
            record_identity_unencrypted_read()
            log.warning("the stored identity of %s is not encrypted yet: run `python -m smo_shared.module_identity encrypt-existing`", module)
        return stored
    if keys.current is None:
        raise IdentityKeyError(f"the stored identity of {module} is encrypted but {KEY_VARIABLE} is not set: give this process the key (or {KEY_VARIABLE}_FILE)")
    key = keys._by_kid.get(key_of(stored) or "")
    if key is None:
        raise IdentityKeyError(f"the stored identity of {module} is encrypted with a key this process does not have: set {KEY_VARIABLE} to it "
                               f"(or, after a rotation, {PREVIOUS_KEY_VARIABLE})")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.urlsafe_b64decode(stored[len(PREFIX):].partition(":")[2])
        plain = AESGCM(key.material).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(module)).decode()
    except (InvalidTag, ValueError) as exc:
        raise IdentityKeyError(f"the stored identity of {module} cannot be decrypted with {KEY_VARIABLE}: wrong key, or the value was changed") from exc
    if key is not keys.current and not quiet:
        record_identity_unencrypted_read("previous-key")
        log.warning("the stored identity of %s is sealed with the previous key: run `python -m smo_shared.module_identity encrypt-existing`", module)
    return plain


def check_configuration(environ: Mapping[str, str] = os.environ) -> None:
    """Fails fast (IdentityKeyError) when the key variables are set wrongly, so a process refuses to start instead of failing at the first
    identity it reads. Called by `install_health` and the worker."""
    load_keys(environ)
