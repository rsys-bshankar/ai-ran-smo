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

The secret is stored as issued, because the module has to present it to SME's token
endpoint (the GUI BFF does the same with `gui_smo_credential`). Anything that can read
this table can act as the module at SME; that is the trust the shared database already
carries, and `DB-2` (per-module schemas and roles) is where it would be narrowed.

`SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` in the environment still win, for deployments
that provision the identity from a secret store.
"""

import datetime

from sqlalchemy import DateTime, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class ModuleIdentityRow(Base):
    """The stored SME invoker identity of one module, shared by all its replicas: one row per `module`, holding the invoker id and the secret as
    issued.

    The secret is kept in clear because the module must present it to SME's token endpoint (see the module description for the trust this implies).
    """
    __tablename__ = "module_identity"

    module: Mapped[str] = mapped_column(String, primary_key=True)          # the MODULE build arg, e.g. "aimgf"
    invoker_id: Mapped[str] = mapped_column(String, nullable=False)
    invoker_secret: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class DbIdentityStore:
    """The shared identity table. `session_factory` defaults to the process's own database session."""

    def __init__(self, session_factory=None):
        self._factory = session_factory

    def _session(self):
        if self._factory is not None:
            return self._factory()
        from .db import SessionLocal  # resolved at call time, so importing this module opens nothing
        return SessionLocal()

    def load(self, module: str) -> tuple[str, str] | None:
        """Returns (invoker id, secret) stored for `module`, or None when no replica has registered one yet."""
        with self._session() as db:
            row = db.get(ModuleIdentityRow, module)
            return (row.invoker_id, row.invoker_secret) if row is not None else None

    def insert(self, module: str, invoker_id: str, secret: str) -> bool:
        """True if this call stored the identity; False if the module already has one."""
        with self._session() as db:
            db.add(ModuleIdentityRow(module=module, invoker_id=invoker_id, invoker_secret=secret))
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

    def put(self, module: str, invoker_id: str, secret: str) -> None:
        """Stores the identity under `module`, replacing any (an rApp instance's credentials pushed by rApp Management: the push is the truth,
        there is no race to win)."""
        with self._session() as db:
            row = db.get(ModuleIdentityRow, module)
            if row is None:
                db.add(ModuleIdentityRow(module=module, invoker_id=invoker_id, invoker_secret=secret))
            else:
                row.invoker_id, row.invoker_secret, row.created_at = invoker_id, secret, _now()
            try:
                db.commit()
            except IntegrityError:            # another replica inserted it at the same moment: write over it
                db.rollback()
                db.execute(update(ModuleIdentityRow).where(ModuleIdentityRow.module == module)
                           .values(invoker_id=invoker_id, invoker_secret=secret, created_at=_now()))
                db.commit()

    def replace(self, module: str, stale_invoker_id: str, invoker_id: str, secret: str) -> bool:
        """Compare-and-swap: True only if the stored identity was still `stale_invoker_id`."""
        with self._session() as db:
            done = db.execute(update(ModuleIdentityRow)
                              .where(ModuleIdentityRow.module == module, ModuleIdentityRow.invoker_id == stale_invoker_id)
                              .values(invoker_id=invoker_id, invoker_secret=secret, created_at=_now()))
            db.commit()
            return done.rowcount == 1
