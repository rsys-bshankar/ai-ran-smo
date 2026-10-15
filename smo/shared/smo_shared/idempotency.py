"""Idempotency keys for command routes (PR-ST-3).

A client that times out, or whose first attempt lost a write race (409
`CONCURRENT_MODIFICATION`, `versioning.py`), cannot tell whether a command ran.
Repeating a create then makes a second instance, deployment or job. With an
`Idempotency-Key` header the platform answers the repeat with the first
attempt's response instead of running the command again.

Use `@idempotent("<module>", status_code=...)` under the route decorator; the
route must take `request: Request` and `db: Session` parameters:

    @app.post("/instances", status_code=202)
    @idempotent("rapp-mgmt", status_code=202)
    def create_instance(body: CreateInstanceRequest, request: Request, db: Session = Depends(get_session)): ...

Behaviour, per (module, caller, key); the caller is the invoker id R1 Termination
vouches for (`invoker.py`), so two rApps cannot replay or collide on each other's keys:

  no header            the route runs as before.
  first use            a reservation row is committed, the route runs, and a 2xx
                       answer is stored with its status code and body.
  repeat, same request the stored answer is returned, with `Idempotent-Replayed: true`.
  repeat, other request  422 IDEMPOTENCY_KEY_REUSED: the key was used for a different
                       method, path or payload (they are hashed together).
  repeat while running 409 IDEMPOTENCY_KEY_IN_PROGRESS: the first attempt has not finished;
                       repeat later. A reservation older than IDEMPOTENCY_IN_PROGRESS_SECONDS
                       (default 300) is treated as abandoned by a crashed replica and taken over.
  failure              an attempt that raises (any 4xx/5xx, or a conflict) releases its
                       reservation, so the repeat runs the command again; only 2xx answers
                       are stored.

Records expire after IDEMPOTENCY_KEY_TTL_SECONDS (default 86400) and are purged when a
new key is reserved. Known limit: the reservation and the stored answer are committed
around the route's own commit, not inside it, so a replica that dies between the route's
commit and the stored answer leaves a reservation that is taken over after the in-progress
timeout; the command can then run a second time. Sync routes only.
"""

import datetime
import functools
import hashlib
import inspect
import json
import os


from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import JSON, DateTime, Integer, String, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base
from .errors import FrameworkError, framework_error
from .invoker import invoker_id
from .timeutil import as_utc

HEADER_NAME = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
MAX_KEY_LENGTH = 255
IN_PROGRESS, COMPLETED = "IN_PROGRESS", "COMPLETED"


def _seconds(env_name: str, default: int) -> int:
    return int(os.environ.get(env_name, default))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class IdempotencyKey(Base):
    """One row per (module, caller, key): the reservation made when a command with an `Idempotency-Key` starts and, once it succeeds, the stored
    answer.

    `scope` is the invoker id (`invoker.invoker_id`), or "anonymous" without one, so keys of different callers never meet. `request_hash`
    fingerprints method, path and payload; `state` is IN_PROGRESS or COMPLETED; `response_status`/`response_body` are filled only on COMPLETED.
    `created_at` is the start time of the reservation, and is also what a takeover of an abandoned key rewrites. Purged after
    IDEMPOTENCY_KEY_TTL_SECONDS.
    """
    __tablename__ = "idempotency_key"

    module: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, primary_key=True)   # the caller: invoker id, or "anonymous"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)     # IN_PROGRESS | COMPLETED
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | list | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


def request_hash(method: str, path: str, payload) -> str:
    """SHA-256 (hex) of the method, path and payload together, with keys sorted so the same request always hashes the same.

    `default=str` makes an unserialisable value (for example a datetime) hash by its text rather than fail. The key is bound to this hash, so
    reusing a key for any other request is detected.
    """
    canonical = json.dumps({"method": method, "path": path, "payload": payload}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _pk(module: str, scope: str, key: str):
    return (IdempotencyKey.module == module, IdempotencyKey.scope == scope, IdempotencyKey.key == key)


def _replay(row: IdempotencyKey) -> JSONResponse:
    # a COMPLETED row always has its status (_complete writes both together); a missing one is a damaged row, answered as the failure it is
    status_code = row.response_status if row.response_status is not None else 500
    return JSONResponse(status_code=status_code, content=row.response_body, headers={REPLAY_HEADER: "true"})


def _begin(db: Session, module: str, scope: str, key: str, req_hash: str) -> JSONResponse | None:
    """None: this request owns the key and must run. A response: replay it. Raises for a reused or running key."""
    for _ in range(2):  # the second pass only happens when the row vanished between the insert race and the re-read
        row = db.get(IdempotencyKey, (module, scope, key))
        if row is None:
            # Expired records are purged here, when a new key is reserved, rather than by a separate job.
            db.execute(delete(IdempotencyKey).where(
                IdempotencyKey.created_at < _now() - datetime.timedelta(seconds=_seconds("IDEMPOTENCY_KEY_TTL_SECONDS", 86400))))
            db.add(IdempotencyKey(module=module, scope=scope, key=key, request_hash=req_hash, state=IN_PROGRESS))
            try:
                db.commit()
                return None
            except IntegrityError:  # another request reserved the same key first
                db.rollback()
                continue
        if row.request_hash != req_hash:
            raise framework_error(FrameworkError.IDEMPOTENCY_KEY_REUSED,
                                  detail="this Idempotency-Key was used for a different request (method, path or payload)")
        if row.state == COMPLETED:
            return _replay(row)
        started = as_utc(row.created_at)
        if _now() - started > datetime.timedelta(seconds=_seconds("IDEMPOTENCY_IN_PROGRESS_SECONDS", 300)):
            # Takeover of a reservation abandoned by a crashed replica: an UPDATE guarded by the created_at that was read, so when several replicas
            # try at once only one changes a row and the others see rowcount 0.
            taken = db.execute(update(IdempotencyKey).where(*_pk(module, scope, key), IdempotencyKey.created_at == row.created_at)
                               .values(created_at=_now()))
            db.commit()
            if taken.rowcount == 1:  # type: ignore[attr-defined]  # compare-and-swap: exactly one replica takes an abandoned key over
                return None
            db.expire_all()
            continue
        raise framework_error(FrameworkError.IDEMPOTENCY_KEY_IN_PROGRESS,
                              detail="the first request with this Idempotency-Key has not finished; repeat later")
    raise framework_error(FrameworkError.IDEMPOTENCY_KEY_IN_PROGRESS, detail="the Idempotency-Key is being taken over; repeat later")


def _release(db: Session, module: str, scope: str, key: str) -> None:
    db.rollback()
    db.execute(delete(IdempotencyKey).where(*_pk(module, scope, key), IdempotencyKey.state == IN_PROGRESS))
    db.commit()


def _complete(db: Session, module: str, scope: str, key: str, status_code: int, result) -> None:
    """Stores the route's successful answer on the reservation and commits, marking it COMPLETED.

    A `Response` result is stored with its own status and JSON-decoded body (an empty body is stored as null); anything else is stored as
    `jsonable_encoder(result)` with the route's declared `status_code`.
    """
    if isinstance(result, Response):
        status_code, body = result.status_code, (json.loads(bytes(result.body)) if result.body else None)
    else:
        body = jsonable_encoder(result)
    db.execute(update(IdempotencyKey).where(*_pk(module, scope, key))
               .values(state=COMPLETED, response_status=status_code, response_body=body))
    db.commit()


def run_idempotent(request: Request, db: Session, module: str, status_code: int, payload, run):
    """Runs `run()` (the route body) at most once per (module, caller, Idempotency-Key); the engine under `@idempotent`.

    Without the header, just runs it. With an invalid key (empty, over 255 characters, non-printable) answers 422 IDEMPOTENCY_KEY_INVALID. Otherwise
    reserves the key (`_begin`): a stored answer comes back as a replay, a key used for another request is 422 IDEMPOTENCY_KEY_REUSED, one still
    running is 409 IDEMPOTENCY_KEY_IN_PROGRESS. When `run()` raises anything, the reservation is released and the error propagates, so the client
    can repeat; when it returns, the answer is stored. Commits on `db` before and after the route runs (see the known limit in the module
    description).
    """
    key = request.headers.get(HEADER_NAME)
    if key is None:
        return run()
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise framework_error(FrameworkError.IDEMPOTENCY_KEY_INVALID,
                              detail=f"Idempotency-Key must be 1 to {MAX_KEY_LENGTH} printable characters")
    scope = invoker_id(request) or "anonymous"
    replay = _begin(db, module, scope, key, request_hash(request.method, request.url.path, payload))
    if replay is not None:
        return replay
    try:
        result = run()
    # BaseException, not Exception, so the reservation is released for every way the route can end abnormally (cancellation and exit signals
    # included); otherwise the key would stay IN_PROGRESS until the takeover timeout.
    except BaseException:
        _release(db, module, scope, key)
        raise
    _complete(db, module, scope, key, status_code, result)
    return result


def idempotent(module: str, status_code: int = 200, exclude: tuple[str, ...] = ()):
    """Route decorator, see the module docstring. `status_code` is the route's success status, replayed as stored. `exclude` names route parameters
    left out of the request fingerprint: a credential sent in a header is checked by the route, not part of what the request asks for, so a repeat with
    the same body is the same request (and the secret is not hashed into a stored row)."""
    def decorate(fn):
        params = inspect.signature(fn).parameters
        if "request" not in params or "db" not in params:
            raise TypeError(f"{fn.__name__}: an @idempotent route needs `request: Request` and `db: Session` parameters")
        left_out = {"request", "db", *exclude}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            payload = jsonable_encoder({k: v for k, v in kwargs.items() if k not in left_out})
            return run_idempotent(kwargs["request"], kwargs["db"], module, status_code, payload, lambda: fn(*args, **kwargs))
        return wrapper
    return decorate
