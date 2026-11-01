"""The lifecycle hooks a module puts its behaviour in (plan task S1.2): `IMoiLifecycleHandler`.

The router does the generic work of every ProvMnS operation (parse the DN, check the class and its containment, validate the attributes, check the
version, write the row, publish the notification, commit). A module registers one handler per class it serves (`ProvMnS.serve(class, handler)`) and
gets six calls, all inside the request's transaction:

  before_create(ctx, moi)        the instance is about to be inserted; `moi.attributes` are validated
  after_create(ctx, moi)         it was inserted (flushed); its notifyMOICreation is not yet published
  before_modify(ctx, old, new)   the attributes are about to change from `old` to `new` (both validated); also for a createMOI that replaces
  after_modify(ctx, old, new)    they changed (flushed)
  before_delete(ctx, moi)        the instance is about to be deleted; for a subtree, called for every instance, deepest first
  after_delete(ctx, moi)         it was deleted (flushed)

A hook vetoes by raising `MnsError` (TS 28.532 error body; `type_="VETOED"` is the usual code): the transaction is rolled back, nothing is written and
nothing is published, whichever hook raised. A hook may write its own rows through `ctx.session`; they commit or roll back with the change. A hook must
not commit. `after_*` hooks run before the commit, so they can still veto; work that must happen only once the change is durable belongs on the event
bus, which delivers the notification after commit.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Moi:
    """A managed object instance as a hook sees it: its DN, class, id, attributes and version (`version` is 0 for one not yet written)."""

    dn: str
    class_name: str
    moi_id: str
    attributes: dict[str, Any] = field(default_factory=dict)
    version: int = 0


@dataclass(frozen=True)
class MoiContext:
    """What a hook is told about the request: the operation (`createMOI`, `modifyMOIAttributes`, `deleteMOI`), the session of its transaction, and
    the DN the request named (for a subtree delete, the base, not the instance being deleted)."""

    operation: str
    session: Session
    target_dn: str


class IMoiLifecycleHandler:
    """The hooks of one class; every method does nothing by default, so a handler overrides only what it needs (see the module description)."""

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Called before the instance is inserted; raise MnsError to refuse."""

    def after_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Called after the instance was inserted, in the same transaction; raise MnsError to undo."""

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Called before the attributes change; raise MnsError to refuse."""

    def after_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Called after the attributes changed, in the same transaction; raise MnsError to undo."""

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Called before the instance is deleted; raise MnsError to refuse."""

    def after_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Called after the instance was deleted, in the same transaction; raise MnsError to undo."""


DEFAULT_HANDLER = IMoiLifecycleHandler()
