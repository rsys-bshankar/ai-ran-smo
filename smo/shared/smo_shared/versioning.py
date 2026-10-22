"""Optimistic concurrency for rows that carry a lifecycle state (PR-ST-2).

Several replicas of one module may load the same row, fire the same FSM
transition and write it back. Without a check the last writer silently wins
and a transition can be applied twice. `Versioned` adds an integer
`row_version` and makes SQLAlchemy emit every UPDATE as
`... WHERE pk = :pk AND row_version = :loaded_version`; when another session
committed first the UPDATE matches no row and SQLAlchemy raises
`StaleDataError`, which `install_concurrency_handler` turns into a 409
ProblemDetails (`CONCURRENT_MODIFICATION`) instead of a 500.

The check covers every ORM write to the row, not only FSM transitions, so a
concurrent update of any column is detected. A caller that gets the 409 simply
repeats the request: the handler reloads the row, so the repeat either
succeeds or is refused as an illegal transition because the other writer
already moved it (`smo_sdk` does this once, see `sdk/smo_sdk/_common.py`).

Core / bulk statements (`update(...).where(...)`) do not carry the check.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from .correlation import get_correlation_id
from .errors import FrameworkError, ProblemDetails

CONCURRENT_MODIFICATION_TITLE = FrameworkError.CONCURRENT_MODIFICATION[0]


class Versioned:
    """Mixin: `row_version` plus the mapper setting that enforces it.

    Put it before `Base`: `class RAppInstance(Versioned, Base): ...`. The
    migration needs `row_version INTEGER NOT NULL DEFAULT 1` on the table.
    """

    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    @declared_attr.directive
    def __mapper_args__(cls):
        return {"version_id_col": cls.row_version}


def concurrent_modification_response(exc: Exception | None = None) -> JSONResponse:
    """Builds the 409 CONCURRENT_MODIFICATION answer (a JSONResponse, not an exception) in the same `{"detail": ProblemDetails}` envelope
    `errors.problem()` produces.

    `exc` is accepted so it can be used directly as an exception handler body; it is not read.
    """
    title, status = FrameworkError.CONCURRENT_MODIFICATION
    detail = "the resource was modified by another request while this one was running; repeat the request"
    body = ProblemDetails(title=title, status=status, detail=detail, correlationId=get_correlation_id()).model_dump()
    return JSONResponse(status_code=status, content={"detail": body})  # same envelope as errors.problem()


def install_concurrency_handler(app: FastAPI) -> None:
    """Answer a stale write with 409 CONCURRENT_MODIFICATION."""

    @app.exception_handler(StaleDataError)
    async def _stale(request, exc):  # noqa: ARG001
        return concurrent_modification_response(exc)
