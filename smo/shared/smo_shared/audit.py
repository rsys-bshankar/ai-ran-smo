"""Tamper-evident audit of changes (PR-SEC-11): who changed what, when, with what result, in a hash chain.

Every row carries `prev_hash` (the hash of the row before it) and `hash` (SHA-256 over the row's own fields and `prev_hash`), and the rows
are numbered 1, 2, 3 ... by the one `audit_head` row, which is locked while a row is added, so replicas of the writer cannot fork the chain. An edited
row breaks its own hash, a deleted or inserted row breaks the numbering and the link after it, and a removed tail no longer matches the head.

    from smo_shared.audit import record, verify

    record(db, actor="api-invoker-1", role="rapp", action="POST", target="/ran-nf-oam/config-jobs", result="201", correlation_id=cid)
    db.commit()                          # the caller commits: the row and the head move together
    verify(db)                           # None when intact, else {"seq": n, "reason": "..."} for the first broken link

Operating it: `python -m smo_shared.audit verify` (exit 1 when broken) and `python -m smo_shared.audit export [--since SEQ] [--format jsonl|syslog]`
read the database named by `SMO_DATABASE_URL`. R1 Termination writes a row for every authenticated change that reaches it (`R1_AUDIT`, default on;
the request body is never recorded).

What it does not do: someone who can write the database *and* recompute every hash after the edit is not detected by the chain alone. Anchor the
head outside the database: `export` ends with the head's `seq` and `hash` (a `head` line), keep that line somewhere the database's writers cannot reach.
Rows are not purged: retention is by archiving an export, not by deleting.
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
import uuid
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Uuid, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base

log = logging.getLogger(__name__)

GENESIS = "0" * 64
_STREAM: dict[str, Any] = {"yield_per": 500}                                  # rows fetched at a time by verify and export: memory, not behaviour
_JSON: dict[str, Any] = {"sort_keys": True, "ensure_ascii": False}           # one row per line: the same bytes for the same row, readable text kept as text
_FORMATS = ("jsonl", "syslog")
_PROG, _DESCRIPTION = "python -m smo_shared.audit", (__doc__ or "").split("\n")[0]
_VERIFY_HELP, _EXPORT_HELP, _SINCE_HELP = ("check the hash chain; exit 1 when it is broken", "write the rows as JSON lines (or syslog) and the head",
                                           "only rows after this seq")
FIELDS = ("seq", "audit_id", "occurred_at", "actor", "actor_role", "action", "target", "result", "correlation_id", "detail", "prev_hash")


class AuditEntry(Base):
    """One row of the audit hash chain (table `audit_log`): who (`actor`, `actor_role`) did what (`action`, `target`), with which `result`, when.

    `seq` is the row's position in the chain (1, 2, 3 ...; assigned by `record()` from the locked head, never by the database), `prev_hash` is the
    `hash` of row `seq - 1` (`GENESIS` for row 1) and `hash` is `compute_hash()` over every other field. Rows are append-only: nothing in this
    module updates or deletes one, and `verify()` reports any row that was. Written through `record()` or `write_audit()`, never constructed by
    callers.
    """
    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=False)
    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[str] = mapped_column(String, index=True)               # the invoker id R1 vouches for
    actor_role: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)                          # the HTTP method
    target: Mapped[str] = mapped_column(String)                          # module prefix and path, no query, no fragment, no body (`clean_target`)
    result: Mapped[str] = mapped_column(String)                          # the status code, or REFUSED:<code>
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    """The single row (`head_id` is always 1) that names the end of the chain: the `seq` and `hash` of the last `AuditEntry`.

    It is locked (`SELECT ... FOR UPDATE`) while a row is added, which is what serialises writers, and `verify()` compares it with where the rows
    actually end, so a removed tail is detected.
    """
    __tablename__ = "audit_head"
    head_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)     # always 1
    last_seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
    last_hash: Mapped[str] = mapped_column(String(64))


def _iso(moment: datetime.datetime) -> str:
    if moment.tzinfo is None:                                            # SQLite hands a timestamp back without its zone
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_hash(entry: AuditEntry) -> str:
    """The SHA-256 (hex) over a row's own fields and its `prev_hash`; the value stored in `AuditEntry.hash`.

    The fields are serialised as JSON with sorted keys and no spaces so the same row always gives the same bytes, and `occurred_at` goes through
    `_iso()` so a timestamp read back from SQLite (no zone) hashes the same as the one written. Changing the field set or the serialisation breaks
    `verify()` for every row already stored.
    """
    body = {
        "seq": entry.seq, "audit_id": str(entry.audit_id), "occurred_at": _iso(entry.occurred_at), "actor": entry.actor,
        "actor_role": entry.actor_role, "action": entry.action, "target": entry.target, "result": entry.result,
        "correlation_id": entry.correlation_id, "detail": entry.detail, "prev_hash": entry.prev_hash,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _locked_head(db: Session) -> AuditHead:
    """Returns the head row with a row lock held until the caller's transaction ends, creating it first when it does not exist.

    Holding the lock from here to the commit is what makes the numbering gapless and the chain unforkable across replicas. When two first-ever
    writers race to create it, the loser's IntegrityError (inside a savepoint) is swallowed and it re-reads the winner's row.
    """
    head = db.execute(select(AuditHead).where(AuditHead.head_id == 1).with_for_update()).scalar_one_or_none()
    if head is None:                                                     # revision 0021 seeds it; this is for a database made another way
        try:
            with db.begin_nested():
                db.add(AuditHead(head_id=1, last_seq=0, last_hash=GENESIS))
        except IntegrityError:
            pass
        head = db.execute(select(AuditHead).where(AuditHead.head_id == 1).with_for_update()).scalar_one()
    return head


def clean_target(target: str) -> str:
    """The `target` of a row without a query string or a fragment: everything from the first `?` or `#` on is dropped (SEC-15.12).

    The target is a module prefix and a path. A query value can carry a secret or a person's data, and a percent-encoded `?` or `#` in a path (`/dme/x%3Ftoken=...`) arrives at the
    gateway already decoded, so the path itself can hold what looks like a query. The cut is made here, where every row is written, not by each caller. Rows written before this
    change keep what they have: `compute_hash` reads the stored target, so the chain of old rows still verifies.
    """
    return target.partition("?")[0].partition("#")[0]


def record(db: Session, *, actor: str, action: str, target: str, result: str, role: str | None = None,
           correlation_id: str | None = None, detail: dict | None = None, now: datetime.datetime | None = None) -> AuditEntry:
    """Adds one row to the chain in `db`'s transaction (the head row is locked until the caller commits). The `target` is stored without its query string and fragment (`clean_target`)."""
    head = _locked_head(db)
    entry = AuditEntry(
        seq=head.last_seq + 1, audit_id=uuid.uuid4(), occurred_at=now or datetime.datetime.now(datetime.timezone.utc), actor=actor, actor_role=role,
        action=action.upper(), target=clean_target(target), result=result, correlation_id=correlation_id, detail=detail, prev_hash=head.last_hash)
    entry.hash = compute_hash(entry)
    db.add(entry)
    head.last_seq, head.last_hash = entry.seq, entry.hash
    return entry


def verify(db: Session) -> dict | None:
    """None when the chain is intact; else the first problem: {"seq": the row it was found at, "reason": ...}.

    Safe beside writers: the head is read before the rows and again after them, and the rows may end anywhere between the two
    (a row and the head move together, so a writer that commits while the rows are read leaves the chain ahead of the first read
    and behind the second). Rows missing from the end, or rows past the head, still break it.
    """
    first = db.execute(select(AuditHead.last_seq, AuditHead.last_hash).where(AuditHead.head_id == 1)).first()   # columns, not the ORM object: the second read must not be the first one's identity-mapped copy
    first_seq, first_hash = (first.last_seq, first.last_hash) if first else (0, GENESIS)
    expected_seq, previous = 1, GENESIS
    at = {0: GENESIS}                                                    # the hash of the row the first head named
    for entry in db.execute(select(AuditEntry).order_by(AuditEntry.seq).execution_options(**_STREAM)).scalars():
        if entry.seq != expected_seq:
            return {"seq": expected_seq, "reason": f"row {expected_seq} is missing (next row is {entry.seq})"}
        if entry.prev_hash != previous:
            return {"seq": entry.seq, "reason": "prev_hash does not match the hash of the row before"}
        if compute_hash(entry) != entry.hash:
            return {"seq": entry.seq, "reason": "the row was changed: its hash does not match its content"}
        expected_seq, previous = entry.seq + 1, entry.hash
        if entry.seq == first_seq:
            at[first_seq] = entry.hash
    last_seq = expected_seq - 1
    head = db.execute(select(AuditHead.last_seq, AuditHead.last_hash).where(AuditHead.head_id == 1)).first()
    if head is None:
        return None if last_seq == 0 else {"seq": last_seq, "reason": "the head row is missing"}
    if not first_seq <= last_seq <= head.last_seq or at.get(first_seq) != first_hash or (last_seq == head.last_seq and head.last_hash != previous):
        return {"seq": last_seq, "reason": f"the head says the chain ends at row {head.last_seq}; it ends at {last_seq} (rows removed from the end?)"}
    return None


def audit_enabled(environ=os.environ) -> bool:
    return environ.get("R1_AUDIT", "on").strip().lower() not in ("off", "false", "0", "no")


def write_audit(**fields) -> bool:
    """Records one row in its own session and commits; never raises (a failed write is logged and counted, it does not fail the call it describes,
    which has already been answered). Returns whether the row was written."""
    from .db import SessionLocal
    from .metrics import record_audit_write
    try:
        for attempt in range(3):
            try:
                with SessionLocal() as db:
                    record(db, **fields)
                    db.commit()
                break
            except IntegrityError:           # two writers took the same number (a database that does not lock the head row, as SQLite): number again
                if attempt == 2:
                    raise
        record_audit_write("ok")
        return True
    except Exception:
        log.exception("audit: could not record %s %s", fields.get("action"), fields.get("target"))
        record_audit_write("failed")
        return False


def as_dict(entry: AuditEntry) -> dict:
    return {**{f: (str(getattr(entry, f)) if f == "audit_id" else getattr(entry, f)) for f in FIELDS},
            "occurred_at": _iso(entry.occurred_at), "hash": entry.hash}


def export(db: Session, since: int = 0, fmt: str = _FORMATS[0], out=None, hostname: str = "smo") -> None:
    """The rows after `since`, one per line, then a `head` line (the anchor to keep elsewhere). `syslog` is RFC 5424 with the JSON as the message."""
    if fmt not in _FORMATS:
        raise ValueError(f"unknown audit export format {fmt!r}: expected one of {', '.join(_FORMATS)}")
    out = out or sys.stdout
    for entry in db.execute(select(AuditEntry).where(AuditEntry.seq > since).order_by(AuditEntry.seq).execution_options(**_STREAM)).scalars():
        line = json.dumps(as_dict(entry), **_JSON)
        if fmt == "syslog":              # facility 13 (log audit), severity 6 (informational)
            line = f"<110>1 {_iso(entry.occurred_at)} {hostname} smo-audit - {entry.seq} - {line}"
        out.write(line + "\n")
    head = db.execute(select(AuditHead).where(AuditHead.head_id == 1)).scalar_one_or_none()
    out.write(json.dumps({"head": {"seq": head.last_seq if head else 0, "hash": head.last_hash if head else GENESIS}}) + "\n")


def main(argv=None) -> int:
    """Entry point of `python -m smo_shared.audit verify|export`; returns the process exit code.

    `verify` prints the verdict and returns 1 when the chain is broken (a script can fail on it); `export` writes the rows and the head to stdout
    and returns 0. Opens its own session on the database named by `SMO_DATABASE_URL`.
    """
    parser = argparse.ArgumentParser(prog=_PROG, description=_DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help=_VERIFY_HELP)
    exp = sub.add_parser("export", help=_EXPORT_HELP)
    exp.add_argument("--since", type=int, default=0, help=_SINCE_HELP)
    exp.add_argument("--format", choices=_FORMATS, default=_FORMATS[0])
    args = parser.parse_args(argv)
    from .db import SessionLocal
    with SessionLocal() as db:
        if args.command == "verify":
            problem = verify(db)
            print("audit chain intact" if problem is None else f"audit chain BROKEN at row {problem['seq']}: {problem['reason']}")
            return 0 if problem is None else 1
        export(db, since=args.since, fmt=args.format)
        return 0


if __name__ == "__main__":
    sys.exit(main())
