"""Transactional notification outbox (PR-MSG-1.2 to 1.4).

A notification to a caller-supplied destination used to be an inline `post_webhook` in the middle of a request: if the
process died after the change committed and before (or while) the notification went out, the notification was lost, and
if the request rolled back after the notification went out, a consumer had been told about something that never
happened. The outbox fixes both: the notification is a row written in the same transaction as the change, and is sent
only after that transaction commits.

    from smo_shared.outbox import enqueue

    def change_something(db):
        ...                                           # the change
        enqueue(db, destination, {"eventType": "X"})  # inserted in the caller's transaction
        db.commit()                                   # the row exists exactly when the change does; sent right after

  enqueue(db, destination, payload, module=None, method="POST")
        Adds a PENDING row to `db`'s transaction (nothing is sent, nothing is flushed). A destination the SSRF guard
        refuses is dropped with a warning, as `post_webhook` always did, and None is returned. `module` defaults to
        the container's `MODULE`. `method` is `POST` (the payload is the JSON body) or `DELETE` (a command whose answer nothing reads, such as
        telling a producer to stop a job: the payload is `{}`); the row is sent, retried and killed the same way either way. Rolling
        the transaction back removes the row.
  drain(engine, ids=None, now=None, limit=100)
        Sends rows that are PENDING and due, oldest first, and records the outcome. With `ids` only those rows (what
        the commit hook below passes); without, every due row (what a worker or a recovery sweep does, MSG-2). A row
        is claimed with one atomic UPDATE that moves its `next_attempt_at` a lease ahead, so two replicas never send
        the same row and a process that dies mid-send leaves a row that becomes due again when the lease runs out
        (delivery is at least once). The claimed rows are sent concurrently (`SMO_OUTBOX_SEND_CONCURRENCY`, default 8), so a destination that does not answer holds up only its own rows, not every row behind it. A 2xx-4xx answer marks it SENT (a 4xx is the destination refusing, which a retry
        will not change); no answer or a 5xx counts an attempt and schedules the next with a backoff; after
        `MAX_ATTEMPTS` the row is DEAD, and so is a row whose destination the SSRF guard refuses at send time.
        Returns {"sent": n, "retry": n, "dead": n}.
  inline drain
        After every commit of a session that enqueued, the rows that session enqueued are drained in the same thread
        (a SQLAlchemy `after_commit` listener), which is what the inline `post_webhook` did, minus the two failure
        modes above. It never raises into the caller. Failed rows are not retried inline, so a dead destination does
        not slow every later request; they wait for the next full `drain`, which is MSG-2's worker.
        `SMO_OUTBOX_INLINE_DRAIN=false` turns the inline drain off (a worker does all the sending, MSG-2.5).

SENT rows are kept for `SMO_OUTBOX_SENT_RETENTION_SECONDS` (default 86400) for the delivery log (MSG-5) and then removed by a
full drain. The table is created by schema revision `0002`; its `method` column by `0005`.
"""

import contextvars
import datetime
import logging
import os
import uuid

from concurrent.futures import ThreadPoolExecutor
from typing import cast

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid, delete, event, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import webhook
from .correlation import correlation_scope, get_correlation_id
from .db import Base

log = logging.getLogger(__name__)

PENDING, SENT, DEAD = "PENDING", "SENT", "DEAD"
METHODS = ("POST", "DELETE")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60
_BACKOFF_SECONDS = (5, 30, 120, 600)          # after attempt 1, 2, 3, 4
_PENDING_IDS_KEY = "outbox_pending_ids"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class NotificationOutbox(Base):
    """One notification waiting to go to a caller-supplied URL (table `notification_outbox`).

    `status` is PENDING (to send, or to retry when `next_attempt_at` is due), SENT or DEAD. `attempts` counts claims, `next_attempt_at` is both the
    retry time and, while a send is in flight, the end of the lease that stops another replica sending it. `module` is the module that enqueued it
    (the metrics and the worker filter on it); `method` is POST (payload is the body) or DELETE.
    """
    __tablename__ = "notification_outbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    module: Mapped[str] = mapped_column(String, nullable=False)          # the module that enqueued it
    destination: Mapped[str] = mapped_column(String, nullable=False)     # the caller-supplied URL
    payload: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False, default="POST", server_default="POST")   # POST | DELETE (MSG-1.10)
    status: Mapped[str] = mapped_column(String, nullable=False, default=PENDING)   # PENDING | SENT | DEAD
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    # The id of the request or worker task that enqueued it (revision 0059), sent with the notification as X-Correlation-ID (call flow 14c).
    correlation_id: Mapped[str | None] = mapped_column(String)


def enqueue(db: Session, destination: str | None, payload: dict, module: str | None = None,
            method: str = "POST") -> NotificationOutbox | None:
    """Adds a PENDING row to `db`'s transaction and returns it; sends nothing and flushes nothing.

    Returns None (and logs a warning when a destination was given) when the SSRF guard refuses `destination`, so a bad destination never reaches the
    table. Raises ValueError for a `method` other than POST or DELETE. The row's id is remembered on the session so the commit hook can drain
    exactly the rows this transaction added; a rollback forgets them and removes the rows.
    """
    if method not in METHODS:
        raise ValueError(f"outbox method must be one of {METHODS}, not {method!r}")
    if not webhook.is_safe_webhook_destination(destination):
        if destination:
            log.warning("webhook destination %r rejected by SSRF guard; notification dropped", destination)
        return None
    now = _now()
    row = NotificationOutbox(id=uuid.uuid4(), module=module or os.environ.get("MODULE", "unknown"), destination=destination,
                             payload=payload, method=method, status=PENDING, attempts=0, next_attempt_at=now, created_at=now,
                             correlation_id=get_correlation_id())
    db.add(row)
    db.info.setdefault(_PENDING_IDS_KEY, []).append(row.id)
    return row


def _send_concurrency() -> int:
    return int(os.environ.get("SMO_OUTBOX_SEND_CONCURRENCY", "8"))


def _backoff(attempts: int) -> datetime.timedelta:
    return datetime.timedelta(seconds=_BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS)) - 1])


def _claim(db: Session, row_id: uuid.UUID, now: datetime.datetime) -> bool:
    """One atomic compare-and-set: only the caller whose UPDATE matches the still-due PENDING row owns this send."""
    claimed = cast(CursorResult, db.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.id == row_id, NotificationOutbox.status == PENDING, NotificationOutbox.next_attempt_at <= now)
        .values(attempts=NotificationOutbox.attempts + 1, next_attempt_at=now + datetime.timedelta(seconds=LEASE_SECONDS))
        .execution_options(synchronize_session=False)))   # the WHERE is the database's to evaluate (SQLite hands datetimes back naive)
    db.commit()
    return claimed.rowcount == 1


def _send(destination: str, payload, method: str = "POST") -> tuple[bool, str | None, bool]:
    """(delivered, error, retryable). The one place a row goes out, so tests can stand in for the network."""
    if not webhook.is_safe_webhook_destination(destination):
        return False, "destination rejected by the SSRF guard", False
    if method == "DELETE":
        response = webhook.delete_webhook(destination, timeout=2.0)
    else:
        response = webhook.post_webhook(destination, json=payload, timeout=2.0)
    if response is None:
        return False, "no answer from the destination", True
    if response.status_code >= 500:
        return False, f"destination answered {response.status_code}", True
    if response.status_code >= 400:
        log.warning("notification to %s refused with %s; not retried", destination, response.status_code)
    return True, None, False


def _send_row(row: NotificationOutbox) -> tuple[bool, str | None, bool]:
    """Sends one claimed row under the correlation id it was enqueued with, so the delivery carries the id of what caused it, whichever process or
    thread sends it (call flow 14c). A row from before revision 0059 has none and goes out under the sender's own."""
    if not row.correlation_id:
        return _send(row.destination, row.payload, row.method)
    with correlation_scope(row.correlation_id):
        return _send(row.destination, row.payload, row.method)


def drain(engine, ids: list[uuid.UUID] | None = None, now: datetime.datetime | None = None, limit: int = 100) -> dict[str, int]:
    """Sends due PENDING rows, oldest first, and records each outcome; returns {"sent": n, "retry": n, "dead": n}.

    `ids` limits it to those rows (the inline drain after a commit); `None` drains every due row up to `limit` and then deletes SENT rows older than
    `SMO_OUTBOX_SENT_RETENTION_SECONDS` (default 86400). Opens its own session on `engine`, so it never touches the caller's transaction. Each row
    is claimed by an atomic UPDATE first (`_claim`); a row another replica claimed is skipped. Claimed rows are sent in parallel threads. Delivered
    or refused with a 4xx: SENT. No answer or a 5xx: retried with backoff until MAX_ATTEMPTS, then DEAD; a destination the SSRF guard refuses at
    send time is DEAD at once. Commits after each row's outcome, so a crash loses at most the rows still in flight, which return after their lease.
    """
    result = {"sent": 0, "retry": 0, "dead": 0}
    now = now or _now()
    with Session(engine, expire_on_commit=False) as db:
        query = (select(NotificationOutbox.id).where(NotificationOutbox.status == PENDING, NotificationOutbox.next_attempt_at <= now)
                 .order_by(NotificationOutbox.created_at, NotificationOutbox.id).limit(limit))
        if ids is not None:
            query = query.where(NotificationOutbox.id.in_(ids))
        claimed = []
        for row_id in db.scalars(query).all():
            # Another replica won the atomic claim: skip the row; it is not this process's to send.
            if not _claim(db, row_id, now):
                continue                                    # another replica took it
            row = db.get(NotificationOutbox, row_id)
            if row is None:
                continue                                    # gone since the claim (purged by hand): nothing to send
            claimed.append(row)
        # sent concurrently: one after the other, a subscriber that never answers made every row behind it wait out its timeout
        with ThreadPoolExecutor(max_workers=max(1, min(_send_concurrency(), len(claimed) or 1))) as pool:
            # each send runs in a copy of this thread's context, so a row without its own id goes out under the drainer's (a worker task's id)
            contexts = [contextvars.copy_context() for _ in claimed]
            outcomes = list(pool.map(lambda pair: pair[1].run(_send_row, pair[0]), zip(claimed, contexts, strict=True)))
        for row, (delivered, error, retryable) in zip(claimed, outcomes, strict=True):
            if delivered:
                row.status, row.last_error = SENT, None
                result["sent"] += 1
            elif retryable and row.attempts < MAX_ATTEMPTS:
                row.last_error, row.next_attempt_at = error, now + _backoff(row.attempts)
                result["retry"] += 1
            else:
                row.status, row.last_error = DEAD, error
                result["dead"] += 1
                log.warning("notification to %s is dead after %d attempt(s): %s", row.destination, row.attempts, error)
            db.commit()
        # A full drain (the worker's or a recovery sweep) also purges old SENT rows; the inline drain after a commit does not, to stay cheap.
        if ids is None:
            retention = float(os.environ.get("SMO_OUTBOX_SENT_RETENTION_SECONDS", "86400"))
            db.execute(delete(NotificationOutbox).where(
                NotificationOutbox.status == SENT, NotificationOutbox.created_at < now - datetime.timedelta(seconds=retention)
            ).execution_options(synchronize_session=False))
            db.commit()
    return result


def inline_drain_enabled() -> bool:
    return os.environ.get("SMO_OUTBOX_INLINE_DRAIN", "true").strip().lower() not in ("0", "false", "no", "off")


@event.listens_for(Session, "after_commit")
def _drain_what_this_commit_enqueued(session: Session) -> None:
    """SQLAlchemy `after_commit` hook: sends the rows the just-committed transaction enqueued, in the committing thread.

    Never raises (a failure is logged and the rows stay pending for a full drain), so a notification problem cannot fail the commit it follows. A
    no-op when the transaction enqueued nothing or `SMO_OUTBOX_INLINE_DRAIN` is off. Registered on every `Session`, so merely importing this module
    turns it on for the process.
    """
    ids = session.info.pop(_PENDING_IDS_KEY, None)
    if not ids or not inline_drain_enabled():
        return
    try:
        drain(session.get_bind(), ids=ids)
    except Exception:                                        # a notification problem must never fail the caller's commit
        log.exception("inline drain of %d notification(s) failed; they stay pending", len(ids))


@event.listens_for(Session, "after_rollback")
def _forget_what_a_rollback_removed(session: Session) -> None:
    session.info.pop(_PENDING_IDS_KEY, None)
