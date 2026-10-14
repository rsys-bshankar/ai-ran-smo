"""One SME invoker identity per module, shared by every replica (PR-ST-4).

Every module that calls another one does so as a CAPIF API invoker at SME
(`r1_client.py`). Before this, each process onboarded its own invoker, so every
replica and every restart added a registration (an 18-process stack registers 18 per
start, and the registry only grows). SME cannot make registration idempotent: it keeps
only a hash of the secret, so a repeat could not return the secret, and rotating it
would make replicas fight over it. The identity is therefore kept here instead:

  load      the module's stored identity, if any.
  insert    the first replica to register stores its identity; a second replica that
            registered at the same moment loses the primary-key race, discards its own
            registration at SME and adopts the stored one.
  replace   when SME no longer knows the stored invoker (token grant refused), one
            replica swaps in a fresh registration with a compare-and-swap on the old
            invoker id; the others adopt the winner's identity.

The secret is kept recoverable, because the module has to present it to SME's token
endpoint (the GUI BFF does the same with `gui_smo_credential`). Since SEC-15.18 it is
encrypted at rest when `SMO_IDENTITY_KEY` (or `_FILE`) is configured (`identity_secrets.py`:
AES-256-GCM, versioned `enc:v1:` format, a previous key for rotation). `DbIdentityStore` is
the only code that reads or writes the column: every caller gets the plaintext back and every
write seals it. Without a key it is stored as issued, as before, and a plaintext row is
always readable, so an upgrade locks nothing out; `python -m smo_shared.module_identity
encrypt-existing` seals the rows that predate the key. A copy of the database alone no
longer acts as the module at SME; the database together with the key still does, and
`DB-2` (per-module schemas and roles) is where reading the table would be narrowed.

`SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` in the environment still win, for deployments
that provision the identity from a secret store.
"""

import argparse
import datetime
import sys
from collections.abc import Sequence

from sqlalchemy import DateTime, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .identity_secrets import IdentityKeyError, IdentityKeys, is_sealed, key_of, load_keys, seal, unseal


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ModuleIdentityRow(Base):
    """The stored SME invoker identity of one module, shared by all its replicas: one row per `module`, holding the invoker id and the secret.

    The secret is the value as stored: the envelope `enc:v1:...` when a key is configured, else (and for a row older than the key) the secret in
    clear. Never read or write it except through `DbIdentityStore`, which seals and unseals it.
    """
    __tablename__ = "module_identity"

    module: Mapped[str] = mapped_column(String, primary_key=True)          # the MODULE build arg, e.g. "aimgf"
    invoker_id: Mapped[str] = mapped_column(String, nullable=False)
    invoker_secret: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class DbIdentityStore:
    """The shared identity table. `session_factory` defaults to the process's own database session; `keys` to what the environment configures."""

    def __init__(self, session_factory=None, keys: IdentityKeys | None = None):
        """`keys` defaults to `load_keys()`, read now: an unusable key configuration raises IdentityKeyError here, not at the first identity."""
        self._factory = session_factory
        self._keys = load_keys() if keys is None else keys

    def _session(self):
        if self._factory is not None:
            return self._factory()
        from .db import SessionLocal  # resolved at call time, so importing this module opens nothing
        return SessionLocal()

    def load(self, module: str) -> tuple[str, str] | None:
        """Returns (invoker id, secret) stored for `module`, or None when no replica has registered one yet. Raises IdentityKeyError when the secret
        is encrypted and this process cannot decrypt it (the message names the variable)."""
        with self._session() as db:
            row = db.get(ModuleIdentityRow, module)
            return (row.invoker_id, unseal(self._keys, module, row.invoker_secret)) if row is not None else None

    def insert(self, module: str, invoker_id: str, secret: str) -> bool:
        """True if this call stored the identity; False if the module already has one."""
        with self._session() as db:
            db.add(ModuleIdentityRow(module=module, invoker_id=invoker_id, invoker_secret=seal(self._keys, module, secret)))
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

    def put(self, module: str, invoker_id: str, secret: str) -> None:
        """Stores the identity under `module`, replacing any (an rApp instance's credentials pushed by rApp Management: the push is the truth,
        there is no race to win)."""
        stored = seal(self._keys, module, secret)
        with self._session() as db:
            row = db.get(ModuleIdentityRow, module)
            if row is None:
                db.add(ModuleIdentityRow(module=module, invoker_id=invoker_id, invoker_secret=stored))
            else:
                row.invoker_id, row.invoker_secret, row.created_at = invoker_id, stored, _now()
            try:
                db.commit()
            except IntegrityError:            # another replica inserted it at the same moment: write over it
                db.rollback()
                db.execute(update(ModuleIdentityRow).where(ModuleIdentityRow.module == module)
                           .values(invoker_id=invoker_id, invoker_secret=stored, created_at=_now()))
                db.commit()

    def replace(self, module: str, stale_invoker_id: str, invoker_id: str, secret: str) -> bool:
        """Compare-and-swap: True only if the stored identity was still `stale_invoker_id`."""
        stored = seal(self._keys, module, secret)
        with self._session() as db:
            done = db.execute(update(ModuleIdentityRow)
                              .where(ModuleIdentityRow.module == module, ModuleIdentityRow.invoker_id == stale_invoker_id)
                              .values(invoker_id=invoker_id, invoker_secret=stored, created_at=_now()))
            db.commit()
            return done.rowcount == 1

    def encrypt_existing(self) -> dict[str, int]:
        """Seals every row that is not sealed with the current key, one row at a time, and returns the counts (the command's output):

        `encrypted` plaintext rows now sealed, `resealed` rows moved from the previous key to the current one, `already` rows that were done,
        `changed` rows a replica rewrote while this ran (left to that write, which is sealed), `unreadable` rows that do not decrypt with any key
        given (left as they are). Idempotent: a second run changes nothing. Raises IdentityKeyError when no key is configured.
        """
        current = self._keys.current
        if current is None:
            raise IdentityKeyError("SMO_IDENTITY_KEY is not set: there is no key to encrypt with (SMO_IDENTITY_KEY or SMO_IDENTITY_KEY_FILE)")
        counts = {"encrypted": 0, "resealed": 0, "already": 0, "changed": 0, "unreadable": 0}
        with self._session() as db:
            rows = [(m, v) for m, v in db.execute(select(ModuleIdentityRow.module, ModuleIdentityRow.invoker_secret).order_by(ModuleIdentityRow.module))]
        for module, stored in rows:
            if key_of(stored) == current.kid:
                counts["already"] += 1
                continue
            try:
                plain = unseal(self._keys, module, stored, quiet=True)
            except IdentityKeyError:
                counts["unreadable"] += 1
                continue
            with self._session() as db:       # compare-and-swap on the value read: a replica's own write in between wins
                done = db.execute(update(ModuleIdentityRow).where(ModuleIdentityRow.module == module, ModuleIdentityRow.invoker_secret == stored)
                                  .values(invoker_secret=seal(self._keys, module, plain)))
                db.commit()
            counts["changed" if done.rowcount != 1 else "resealed" if is_sealed(stored) else "encrypted"] += 1
        return counts


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m smo_shared.module_identity encrypt-existing`: seals the stored identities with `SMO_IDENTITY_KEY` and prints the counts. Exit 0 when
    every row is sealed, 1 when some row could not be read with the keys given, 2 for a missing or unusable key. Never prints a key or a secret.
    """
    parser = argparse.ArgumentParser(prog="python -m smo_shared.module_identity", description="Maintenance of the stored module identities (SEC-15.18).")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "encrypt-existing", help="seal every stored identity with the current key (also moves rows from the previous key); safe to run again")
    parser.parse_args(argv)
    try:
        counts = DbIdentityStore().encrypt_existing()
    except IdentityKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(" ".join(f"{name}={number}" for name, number in counts.items()))
    return 1 if counts["unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
