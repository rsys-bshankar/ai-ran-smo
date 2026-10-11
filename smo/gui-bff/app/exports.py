"""Asynchronous CSV export jobs (GUI-9.5b): `POST /api/exports`, `GET /api/exports[/{id}[/file]]`, `DELETE /api/exports/{id}`.

The streamed exports (`GET /api/admin/audit.csv` here, RAN NF OAM's `GET /decision-records/export.csv`) hold one request open for the whole file,
and are bounded to 1,000,000 rows (and the decisions to 31 days) for that reason. A job instead runs as an asyncio task in the BFF instance that
accepted it, pages its source by keyset and appends the CSV to the database in chunks of at most `CHUNK_BYTES`; the browser asks how far it is
and downloads the file when it is DONE, from any instance on the same GUI_DATABASE_URL (the chunks are rows of `gui_export_chunk`, app/db.py).

- Sources: `decisions` (role operator) pages RAN NF OAM's `GET /decision-records?after=<cursor>` through the gateway, newest first, `DECISION_PAGE`
  rows a call, with the filters `since`, `until`, `invoker_id`, `disposition`, `region`, `site_cluster`; `alarms` (role operator, GUI-2.5) pages
  `GET /alarms?after=<cursor>` the same way, in the alarm console's order (most severe, then newest), with its filters `since` / `until` (raisedAt),
  `severity`, `ack_state`, `open_only`, `probable_cause`, `managed_element_ref`, `managed_function_ref`, `region`, `site_cluster`; `audit` (role admin) reads the BFF's own
  `gui_audit_log` by id, newest first, with main.py's `audit_query` (`since`, `until`, `username`, `action`). No 31-day bound; at most `MAX_ROWS`
  rows (a job that reaches it ends DONE with `error` saying the file stops there); at most `MAX_ACTIVE_PER_USER` QUEUED or RUNNING jobs per user.
- The columns are those of the streamed exports, and a cell a spreadsheet would run as a formula gets a leading `'` (main.py's `_csv_cell`).
- Orphans: the task writes `heartbeat_at` at every page. A QUEUED or RUNNING job whose heartbeat is older than `STALE_SECONDS`, or that names this
  process as its runner but has no task here, lost its instance (a restart, a crash): it is marked FAILED "interrupted" when it is next read. A
  stopping instance marks its own jobs so at once (`ExportRunner.shutdown`, from main.py's lifespan).
- Lifetime: a job expires `TTL` after it finished (or was created, until then); an expired job is shown as EXPIRED, its file is a 410, and the next
  `POST /api/exports` (any user's) deletes every expired job with its chunks.
- Audited: `EXPORT_REQUESTED` (id, kind, filters), `EXPORT_DOWNLOADED`, `EXPORT_DELETED`.

Installed by main.py after the admin routes (it needs `audit_query`). The task is the one deliberate background work of this module, listed in
scripts/statelessness_allowlist.txt and docs/ARCHITECTURE.md ("Process state and scale-out"): the job's state is in the database, the process only
does the work.
"""

import asyncio
import csv
import datetime
import io
import logging
import uuid
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable, Iterator, Literal

import httpx
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from starlette.concurrency import run_in_threadpool

from . import scoping
from .db import AuditEntry, ExportChunk, ExportJob, GuiUser
from .rbac import RANK, Role
from .smo_client import SmoAuthError
from .summary import SCOPE_RE

log = logging.getLogger("smo-gui-bff")

CHUNK_BYTES = 1024 * 1024
MAX_ROWS = 10_000_000
MAX_ACTIVE_PER_USER = 3
TTL = datetime.timedelta(hours=24)
STALE_SECONDS = 90.0            # well above the longest gap between two heartbeats: one source call (FETCH_TIMEOUT_SECONDS) plus a chunk write
DECISION_PAGE = 500             # RAN NF OAM's largest page (smo_shared/pagination.py MAX_LIMIT)
AUDIT_BATCH = 1000
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_ATTEMPTS = 3
ACTIVE = ("QUEUED", "RUNNING")
INTERRUPTED = "interrupted: the BFF instance running this export stopped before it finished"
KIND_ROLES = {"decisions": Role.OPERATOR, "alarms": Role.OPERATOR, "audit": Role.ADMIN}
DECISIONS_PATH = "/ran-nf-oam/decision-records"
ALARMS_PATH = "/ran-nf-oam/alarms"
# The columns of RAN NF OAM's own decision export (ran-nf-oam/app/main.py DECISION_CSV_COLUMNS), so both files read the same.
DECISION_COLUMNS = ("decisionId", "occurredAt", "invokerId", "requestedBy", "disposition", "jobId", "approvalId", "actionId", "inputsRef",
                    "modelVersion", "rationale", "decidedBy", "decidedAt", "managedElements", "changeCount", "correlationId", "contentHash", "auditSeq")
DISPOSITIONS = Literal["DIRECT", "APPROVED", "ROLLBACK", "REJECTED", "EXPIRED", "REFUSED"]
# GUI-2.5: the alarm fields an operator reads in the console, in the order of its table and detail panel (ran-nf-oam/app/main.py `_alarm_view`).
ALARM_COLUMNS = ("alarmId", "raisedAt", "severity", "managedElementRef", "managedFunctionRef", "probableCause", "specificProblem", "alarmType",
                 "ackState", "ackTime", "ackUserId", "clearTime", "clearUserId", "sourceAlarmId", "correlationGroup", "rootCauseIndicator",
                 "proposedRepairActions")
SEVERITIES = Literal["critical", "major", "minor", "warning", "indeterminate", "cleared"]
# The filters each kind takes (the others are refused with 422, so a filter is never silently ignored); `since` / `until` apply to every kind.
KIND_FILTERS = {
    "decisions": ("invokerId", "disposition", "region", "siteCluster"),
    "alarms": ("severity", "ackState", "openOnly", "probableCause", "managedElementRef", "managedFunctionRef", "region", "siteCluster"),
    "audit": ("username", "action"),
}
ALL_FILTERS = tuple(dict.fromkeys(f for fs in KIND_FILTERS.values() for f in fs))


# GUI-5: the identity headers of the export the running task writes (the person who asked and their scope claim), set by `run` for its own task: a context variable,
# because one runner writes several jobs at once
_job_headers: ContextVar[dict[str, str] | None] = ContextVar("_job_headers", default=None)

class ExportError(RuntimeError):
    """A job cannot go on: the source refused or did not answer. The message is stored in the job's `error`."""


class ExportRequest(BaseModel):
    """What to export. `since` is required and `until` defaults to the time of the request (both kept in the job, so the file is that span
    whatever is written later). The other filters belong to one or two kinds (`KIND_FILTERS`): `invokerId`, `disposition` to `decisions`;
    `severity`, `ackState`, `openOnly`, `probableCause`, `managedElementRef`, `managedFunctionRef` to `alarms`; `region`, `siteCluster` to both;
    `username`, `action` to `audit`."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["decisions", "alarms", "audit"]
    since: datetime.datetime
    until: datetime.datetime | None = None
    invokerId: str | None = Field(None, min_length=1, max_length=200)
    disposition: DISPOSITIONS | None = None
    username: str | None = Field(None, min_length=1, max_length=200)
    action: str | None = Field(None, min_length=1, max_length=64)
    region: str | None = None
    siteCluster: str | None = None
    severity: SEVERITIES | None = None
    ackState: Literal["ACKNOWLEDGED", "UNACKNOWLEDGED"] | None = None
    openOnly: bool | None = None
    probableCause: str | None = Field(None, min_length=1, max_length=200)
    managedElementRef: str | None = Field(None, min_length=1, max_length=200)
    managedFunctionRef: str | None = Field(None, min_length=1, max_length=500)


def _utc(at: datetime.datetime) -> datetime.datetime:
    """`at` as an aware UTC time; a naive value (SQLite hands them back so, and a caller may send one) is taken as UTC."""
    return at.replace(tzinfo=datetime.UTC) if at.tzinfo is None else at.astimezone(datetime.UTC)


def _stamp(at: datetime.datetime | None) -> str | None:
    """ISO 8601 UTC with a `Z`, or None."""
    return None if at is None else _utc(at).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def file_name(job: ExportJob) -> str:
    """The download's file name: `smo-<kind>-<created, UTC>-<first 8 of the id>.csv`."""
    return f"smo-{job.kind}-{_utc(job.created_at):%Y%m%dT%H%M%SZ}-{str(job.id)[:8]}.csv"


class _ChunkWriter:
    """The CSV text of one job, encoded and appended to `gui_export_chunk` in pieces of exactly `CHUNK_BYTES` (the last one shorter). Holds at
    most one chunk in memory."""

    def __init__(self, db, job_id: uuid.UUID):
        """A writer for the job `job_id` in `db` (app/db.py's Database), starting at chunk 0 with nothing written."""
        self.db, self.job_id = db, job_id
        self.buffer = bytearray()
        self.seq = 0
        self.bytes = 0
        self.text = io.StringIO()
        self.writer = csv.writer(self.text)

    def row(self, values: list[str]) -> None:
        """Add one CSV row (cells already neutralised)."""
        self.writer.writerow(values)

    async def flush(self, final: bool = False) -> None:
        """Move the rows written so far into the buffer and store every full chunk (and, when `final`, the rest)."""
        data = self.text.getvalue().encode("utf-8")
        self.text.seek(0)
        self.text.truncate()
        self.buffer += data
        self.bytes += len(data)
        while len(self.buffer) >= CHUNK_BYTES or (final and self.buffer):
            piece = bytes(self.buffer[:CHUNK_BYTES])
            del self.buffer[:CHUNK_BYTES]
            await run_in_threadpool(self._store, self.seq, piece)
            self.seq += 1

    def _store(self, seq: int, piece: bytes) -> None:
        """Insert one chunk (its own short transaction)."""
        with self.db.session() as s:
            s.add(ExportChunk(job_id=self.job_id, seq=seq, data=piece))
            s.commit()


class ExportRunner:
    """The export tasks of this process. `runner_id` names this process in the jobs it runs; `tasks` holds a reference to each running task (an
    asyncio task with no reference may be collected mid-run) and is how a read tells a live job of this process from one it lost."""

    def __init__(self, app: FastAPI, *, audit_query: Callable, csv_cell: Callable[[Any], str], audit_columns: tuple[str, ...], iso: Callable):
        """The runner of `app` (its database and gateway are read from `app.state` when used), with main.py's helpers (see `install`) and a new
        random `runner_id` for this process."""
        self.app = app
        self.audit_query, self.csv_cell, self.audit_columns, self.iso = audit_query, csv_cell, audit_columns, iso
        self.runner_id = uuid.uuid4().hex
        self.tasks: dict[uuid.UUID, asyncio.Task] = {}
        self.starting: set[uuid.UUID] = set()      # written by the create route, not yet a task: not an orphan

    @property
    def db(self):
        return self.app.state.db

    # ------------------------------------------------------------ job rows

    def settle(self, s, jobs: list[ExportJob]) -> None:
        """Mark FAILED "interrupted" every QUEUED or RUNNING job of `jobs` that lost its instance: a stale heartbeat, or this process as its runner
        and no task here. Updates the objects and commits in `s`. The update is conditional on the state, so a job that ended meanwhile keeps
        its end."""
        now = _now()
        lost = [j for j in jobs if j.state in ACTIVE and (j.heartbeat_at is None or (now - _utc(j.heartbeat_at)).total_seconds() > STALE_SECONDS
                                                          or (j.runner_id == self.runner_id and j.id not in self.tasks and j.id not in self.starting))]
        for job in lost:
            done = s.execute(update(ExportJob).where(ExportJob.id == job.id, ExportJob.state.in_(ACTIVE))
                             .values(state="FAILED", error=INTERRUPTED, finished_at=now, expires_at=now + TTL)).rowcount
            if done:
                job.state, job.error, job.finished_at, job.expires_at = "FAILED", INTERRUPTED, now, now + TTL
        if lost:
            s.commit()

    def settle_all(self, s) -> None:
        """`settle` every QUEUED or RUNNING job in the database (before counting a user's active jobs, and before the purge)."""
        self.settle(s, list(s.scalars(select(ExportJob).where(ExportJob.state.in_(ACTIVE))).all()))

    def purge_expired(self, s) -> int:
        """Delete every finished job past `expires_at`, with its chunks, and any chunk whose job is gone (a job deleted while another instance was
        still writing it). Returns how many jobs. Commits."""
        expired = list(s.scalars(select(ExportJob.id).where(ExportJob.expires_at < _now(), ExportJob.state.not_in(ACTIVE))).all())
        if expired:
            s.execute(delete(ExportChunk).where(ExportChunk.job_id.in_(expired)))
            s.execute(delete(ExportJob).where(ExportJob.id.in_(expired)))
        s.execute(delete(ExportChunk).where(ExportChunk.job_id.not_in(select(ExportJob.id))))
        s.commit()
        return len(expired)

    def view(self, job: ExportJob) -> dict:
        """The job as the API shows it; a finished job past `expires_at` is EXPIRED, and `fileUrl` is set only while the file can be downloaded."""
        expired = job.state not in ACTIVE and _utc(job.expires_at) <= _now()
        state = "EXPIRED" if expired else job.state
        return {"id": str(job.id), "kind": job.kind, "username": job.username, "params": job.params, "state": state, "rows": job.rows,
                "bytes": job.bytes, "error": job.error, "createdAt": self.iso(job.created_at), "finishedAt": self.iso(job.finished_at),
                "expiresAt": self.iso(job.expires_at), "fileName": file_name(job),
                "fileUrl": f"/api/exports/{job.id}/file" if state == "DONE" else None}

    # ------------------------------------------------------------ the task

    def start(self, job_id: uuid.UUID) -> None:
        """Run the job as a task on the running loop, keeping a reference until it ends."""
        task = asyncio.get_running_loop().create_task(self.run(job_id), name=f"export-{job_id}")
        self.tasks[job_id] = task
        self.starting.discard(job_id)
        task.add_done_callback(lambda _t: self.tasks.pop(job_id, None))

    def _update(self, job_id: uuid.UUID, **values) -> bool:
        """Set `values` on the job if it is still QUEUED or RUNNING; False when it is gone or ended (deleted, or marked an orphan by a reader)."""
        with self.db.session() as s:
            done = s.execute(update(ExportJob).where(ExportJob.id == job_id, ExportJob.state.in_(ACTIVE)).values(**values)).rowcount
            s.commit()
        return bool(done)

    def _drop_chunks(self, job_id: uuid.UUID) -> None:
        """Delete the chunks of a job that no longer exists (it was deleted while this task wrote it)."""
        with self.db.session() as s:
            if s.get(ExportJob, job_id) is None:
                s.execute(delete(ExportChunk).where(ExportChunk.job_id == job_id))
                s.commit()

    async def run(self, job_id: uuid.UUID) -> None:
        """Write the whole file of the job: header, then every page of its source, chunk by chunk, with the counts and the heartbeat updated after
        each page. Ends DONE (also when `MAX_ROWS` is reached: `error` says so), or FAILED with the reason. Stops quietly when the job was deleted
        or marked an orphan meanwhile. A cancelled task (the instance is stopping) leaves its job FAILED "interrupted". Never raises, except the
        cancellation itself."""
        with self.db.session() as s:
            job = s.get(ExportJob, job_id)
            kind, params = (job.kind, dict(job.params)) if job is not None else (None, {})
            # GUI-5: the rows are read as the person who asked, with their scope claim as it is now (a narrower one since they asked applies; an account deleted
            # since gets a claim that permits nothing, so its export writes no row of anyone's)
            asker = s.get(GuiUser, job.username) if job is not None else None
            _job_headers.set(scoping.headers(job.username, asker.scope if asker is not None else '{"regions":[]}') if job is not None else {})
        if kind is None or not await run_in_threadpool(self._update, job_id, state="RUNNING", heartbeat_at=_now(), runner_id=self.runner_id):
            return
        out = _ChunkWriter(self.db, job_id)
        rows, note = 0, None
        try:
            columns = {"decisions": DECISION_COLUMNS, "alarms": ALARM_COLUMNS}.get(kind, self.audit_columns)
            out.row(list(columns))
            source = {"decisions": self._decisions, "alarms": self._alarms}.get(kind, self._audit)(params)
            async for batch in source:
                for values in batch[:MAX_ROWS - rows]:
                    out.row(values)
                rows += min(len(batch), MAX_ROWS - rows)
                await out.flush()
                alive = await run_in_threadpool(self._update, job_id, rows=rows, bytes=out.bytes, heartbeat_at=_now())
                if not alive:
                    await run_in_threadpool(self._drop_chunks, job_id)
                    return
                if rows >= MAX_ROWS:
                    note = f"stopped at the limit of {MAX_ROWS:,} rows: narrow the filters or the span for the rest"
                    break
            await out.flush(final=True)
            finished = _now()
            if not await run_in_threadpool(self._update, job_id, state="DONE", rows=rows, bytes=out.bytes, error=note, finished_at=finished,
                                           expires_at=finished + TTL, heartbeat_at=finished):
                await run_in_threadpool(self._drop_chunks, job_id)
        except asyncio.CancelledError:
            # The instance is stopping (or the job was deleted here): written synchronously, because the loop may not run another await.
            finished = _now()
            self._update(job_id, state="FAILED", error=INTERRUPTED, finished_at=finished, expires_at=finished + TTL)
            raise
        except Exception as exc:        # deliberate catch-all: whatever went wrong, the job must end FAILED with a reason, not stay RUNNING
            reason = str(exc) if isinstance(exc, ExportError) else f"the export failed: {exc.__class__.__name__}"
            if not isinstance(exc, ExportError):
                log.exception("export %s failed", job_id)
            finished = _now()
            await run_in_threadpool(self._update, job_id, state="FAILED", error=reason, rows=rows, finished_at=finished, expires_at=finished + TTL)

    async def _fetch(self, params: list[tuple[str, str]], path: str = DECISIONS_PATH, what: str = "decision") -> dict:
        """One keyset page of a RAN NF OAM list (`path`: the decision records, or the alarms); retried `FETCH_ATTEMPTS` times on a transport
        error or a 5xx, a short pause between. Raises ExportError when it still fails or the answer is not a keyset page."""
        last = "no answer"
        for attempt in range(FETCH_ATTEMPTS):
            if attempt:
                await asyncio.sleep(attempt)       # 1 s, then 2 s: a module restarting under a rolling upgrade is back by then
            try:
                resp = await self.app.state.gateway.request("GET", path, params=params, timeout=FETCH_TIMEOUT_SECONDS, headers=_job_headers.get() or {})
            except (SmoAuthError, httpx.HTTPError) as exc:
                last = exc.__class__.__name__
                log.warning("export: a %s page failed: %r", what, exc)
                continue
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
                raise ExportError(f"RAN NF OAM refused the {what} page: HTTP {resp.status_code}")
            return body
        raise ExportError(f"RAN NF OAM did not answer the {what} page ({last}) after {FETCH_ATTEMPTS} attempts")

    def _list_cell(self, value: Any) -> str:
        """A field of a decision or an alarm as a cell: a list joined by `;` (as RAN NF OAM's own decision export does), then neutralised."""
        if isinstance(value, list):
            value = ";".join(str(v) for v in value)
        return self.csv_cell(value)

    async def _decisions(self, params: dict) -> AsyncIterator[list[list[str]]]:
        """The decision records of the job's filters, newest first, one list of rows per page, by RAN NF OAM's keyset cursor (`after`)."""
        query = [("limit", str(DECISION_PAGE)), ("since", params["since"]), ("until", params["until"])]
        for key, name in (("invokerId", "invoker_id"), ("disposition", "disposition"), ("region", "region"), ("siteCluster", "site_cluster")):
            if params.get(key):
                query.append((name, params[key]))
        cursor = ""
        while True:
            body = await self._fetch([*query, ("after", cursor)])
            items = [r for r in body["items"] if isinstance(r, dict)]
            yield [[self._list_cell(r.get(c)) for c in DECISION_COLUMNS] for r in items]
            cursor = body.get("nextCursor") or ""
            if not body.get("hasMore") or not cursor or not items:
                return

    async def _alarms(self, params: dict) -> AsyncIterator[list[list[str]]]:
        """GUI-2.5: the alarms of the job's filters in the console order (most severe, then newest), one list of rows per page, by RAN NF OAM's
        keyset cursor (`after`). A list field (the repair actions) is joined by `;` like the decision export's."""
        query = [("limit", str(DECISION_PAGE)), ("since", params["since"]), ("until", params["until"])]
        for key, name in (("severity", "severity"), ("ackState", "ack_state"), ("probableCause", "probable_cause"),
                          ("managedElementRef", "managed_element_ref"), ("managedFunctionRef", "managed_function_ref"),
                          ("region", "region"), ("siteCluster", "site_cluster")):
            if params.get(key):
                query.append((name, params[key]))
        if params.get("openOnly"):
            query.append(("open_only", "true"))
        cursor = ""
        while True:
            body = await self._fetch([*query, ("after", cursor)], ALARMS_PATH, "alarm")
            items = [r for r in body["items"] if isinstance(r, dict)]
            yield [[self._list_cell(r.get(c)) for c in ALARM_COLUMNS] for r in items]
            cursor = body.get("nextCursor") or ""
            if not body.get("hasMore") or not cursor or not items:
                return

    def _audit_batch(self, params: dict, below: int | None) -> list[tuple[int, list[str]]]:
        """One batch of the BFF's audit rows matching the job's filters, newest first, with ids below `below`: (id, cells) pairs."""
        stmt = self.audit_query(params.get("username"), params.get("action"), datetime.datetime.fromisoformat(params["since"]),
                                datetime.datetime.fromisoformat(params["until"]))
        if below is not None:
            stmt = stmt.where(AuditEntry.id < below)
        with self.db.session() as s:
            batch = s.scalars(stmt.limit(AUDIT_BATCH)).all()
            return [(e.id, [self.csv_cell(v) for v in (e.id, self.iso(e.at), e.username, e.role, e.action, e.method, e.path, e.status_code, e.detail)])
                    for e in batch]

    async def _audit(self, params: dict) -> AsyncIterator[list[list[str]]]:
        """The audit rows of the job's filters, newest first, `AUDIT_BATCH` at a time by id (each batch its own short read, off the loop)."""
        below = None
        while True:
            batch = await run_in_threadpool(self._audit_batch, params, below)
            yield [cells for _, cells in batch]
            if len(batch) < AUDIT_BATCH:
                return
            below = batch[-1][0]

    async def shutdown(self) -> None:
        """Cancel this process's tasks (the instance is stopping); each leaves its job FAILED "interrupted"."""
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def install(app: FastAPI, *, current_session: Callable, audit: Callable, problem: Callable[..., JSONResponse], audit_query: Callable,
            csv_cell: Callable[[Any], str], audit_columns: tuple[str, ...], iso: Callable) -> None:
    """Add the export routes to `app` and keep the runner on `app.state.exports` (main.py's lifespan calls its `shutdown`). `audit_query` is
    main.py's filter of the audit log, `csv_cell` its formula-neutralising cell, `audit_columns` the audit CSV header, `iso` its time format."""
    runner = ExportRunner(app, audit_query=audit_query, csv_cell=csv_cell, audit_columns=audit_columns, iso=iso)
    app.state.exports = runner

    def visible(s, job_id: str, session) -> ExportJob | None:
        """The job if it exists and the caller may see it (its owner, or an admin); settled first. None otherwise, so another user's job is a 404."""
        try:
            key = uuid.UUID(job_id)
        except ValueError:
            return None
        job = s.get(ExportJob, key)
        if job is None or (job.username != session.user.username and session.user.role != Role.ADMIN):
            return None
        runner.settle(s, [job])
        return job

    def create(body: ExportRequest, session) -> dict | JSONResponse:
        """Validate, purge the expired jobs, check the caller's active jobs, write the QUEUED row and audit it; the view, or the refusal."""
        needed = KIND_ROLES[body.kind]
        if RANK[session.user.role] < RANK[needed]:
            return problem(403, "FORBIDDEN", f"a {body.kind} export requires role {needed}")
        until = _utc(body.until) if body.until else _now()
        since = _utc(body.since)
        if until <= since:
            return problem(422, "INVALID_EXPORT", "`until` must be after `since`")
        wrong = [f for f in ALL_FILTERS if f not in KIND_FILTERS[body.kind] and getattr(body, f) is not None]
        if wrong:
            return problem(422, "INVALID_EXPORT", f"{', '.join(wrong)} does not apply to a {body.kind} export")
        if any(v is not None and not SCOPE_RE.match(v) for v in (body.region, body.siteCluster)):
            return problem(422, "INVALID_EXPORT", "region and siteCluster are 1-64 characters of A-Z a-z 0-9 . _ -")
        params = {"since": since.isoformat(), "until": until.isoformat()}
        for key in ALL_FILTERS:
            if getattr(body, key) is not None:
                params[key] = getattr(body, key)
        with app.state.db.session() as s:
            runner.settle_all(s)
            runner.purge_expired(s)
            # Counted, then inserted: two requests of one user at the same instant may both pass, a fourth job at worst.
            active = s.scalar(select(func.count()).select_from(ExportJob).where(ExportJob.username == session.user.username,
                                                                                ExportJob.state.in_(ACTIVE)))
            if active >= MAX_ACTIVE_PER_USER:
                return problem(429, "TOO_MANY_EXPORTS", f"at most {MAX_ACTIVE_PER_USER} exports of one user run at a time: wait for one to finish")
            now = _now()
            job = ExportJob(id=uuid.uuid4(), username=session.user.username, kind=body.kind, params=params, state="QUEUED", rows=0, bytes=0,
                            created_at=now, expires_at=now + TTL, runner_id=runner.runner_id, heartbeat_at=now)
            runner.starting.add(job.id)
            s.add(job)
            s.commit()
            view = runner.view(job)
        audit("EXPORT_REQUESTED", session.user, detail=f"{view['id']} {body.kind} " + " ".join(f"{k}={v}" for k, v in params.items()))
        return view

    @app.post("/api/exports", status_code=202)
    async def request_export(body: ExportRequest, session=Depends(current_session)):
        """GUI-9.5b: start an export job; answers 202 with the job (`{id, state: "QUEUED", ...}`) at once, and the file is written in the
        background by this instance. `decisions` and `alarms` (GUI-2.5) need role operator, `audit` admin. No bound on the span; at most 10,000,000 rows; at most 3
        QUEUED or RUNNING jobs per user. Audited (`EXPORT_REQUESTED`). Expired jobs of every user are purged first. 403 `FORBIDDEN`, 422
        `INVALID_EXPORT` (a filter of the other kind, `until` not after `since`, a malformed region), 429 `TOO_MANY_EXPORTS`."""
        result = await run_in_threadpool(create, body, session)
        if isinstance(result, Response):
            return result
        runner.start(uuid.UUID(result["id"]))
        return JSONResponse(status_code=202, content=result)

    @app.get("/api/exports")
    def list_exports(limit: int = Query(50, ge=1, le=200, description="At most this many jobs, newest first."),
                     username: str | None = Query(None, description="Admin only: only this user's jobs."), session=Depends(current_session)):
        """The caller's export jobs, newest first (an admin sees every user's, or one user's with `username`): `{items: [job]}`. A job that lost its
        instance is marked FAILED "interrupted" as it is read; a finished job past its expiry reads EXPIRED."""
        stmt = select(ExportJob).order_by(ExportJob.created_at.desc()).limit(limit)
        if session.user.role != Role.ADMIN:
            stmt = stmt.where(ExportJob.username == session.user.username)
        elif username:
            stmt = stmt.where(ExportJob.username == username)
        with app.state.db.session() as s:
            jobs = list(s.scalars(stmt).all())
            runner.settle(s, jobs)
            return {"items": [runner.view(j) for j in jobs]}

    @app.get("/api/exports/{job_id}")
    def read_export(job_id: str, session=Depends(current_session)):
        """One export job (its owner's or, for an admin, anyone's): state, rows and bytes so far, error, times, `fileUrl` once DONE. 404
        `NO_SUCH_EXPORT` (also another user's job)."""
        with app.state.db.session() as s:
            job = visible(s, job_id, session)
            if job is None:
                return problem(404, "NO_SUCH_EXPORT")
            return runner.view(job)

    @app.get("/api/exports/{job_id}/file", responses={200: {"description": "The export's CSV file", "content": {"text/csv": {}}}})
    def download_export(job_id: str, session=Depends(current_session)):
        """The CSV file of a DONE job, streamed chunk by chunk from the database (any instance serves it), as an attachment. Audited
        (`EXPORT_DOWNLOADED`). 404 `NO_SUCH_EXPORT`, 409 `EXPORT_NOT_READY` (QUEUED, RUNNING or FAILED), 410 `EXPORT_EXPIRED`."""
        with app.state.db.session() as s:
            job = visible(s, job_id, session)
            if job is None:
                return problem(404, "NO_SUCH_EXPORT")
            view = runner.view(job)
        if view["state"] == "EXPIRED":
            return problem(410, "EXPORT_EXPIRED", "the file was kept 24 hours: request the export again")
        if view["state"] != "DONE":
            return problem(409, "EXPORT_NOT_READY", f"the export is {view['state']}" + (f": {job.error}" if job.error else ""))
        audit("EXPORT_DOWNLOADED", session.user, detail=f"{view['id']} {job.kind} rows={job.rows}")
        key = job.id

        def chunks() -> Iterator[bytes]:
            # one short read per chunk, in order, so neither the file nor an open transaction is held for the whole download
            seq = 0
            while True:
                with app.state.db.session() as s:
                    piece = s.get(ExportChunk, (key, seq))
                if piece is None:
                    return
                yield piece.data
                seq += 1

        return StreamingResponse(chunks(), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{view["fileName"]}"', "Content-Length": str(job.bytes)})

    @app.delete("/api/exports/{job_id}", status_code=204)
    def delete_export(job_id: str, session=Depends(current_session)):
        """Delete a job and its file (its owner, or an admin), whatever its state; a job still running stops at its next page (here at once,
        in another instance when it next writes). Audited (`EXPORT_DELETED`). 404 `NO_SUCH_EXPORT`."""
        with app.state.db.session() as s:
            job = visible(s, job_id, session)
            if job is None:
                return problem(404, "NO_SUCH_EXPORT")
            key, kind = job.id, job.kind
            s.execute(delete(ExportChunk).where(ExportChunk.job_id == key))
            s.execute(delete(ExportJob).where(ExportJob.id == key))
            s.commit()
        task = runner.tasks.get(key)
        if task is not None:
            # cancelled on its own loop: this route runs in a worker thread
            task.get_loop().call_soon_threadsafe(task.cancel)
        audit("EXPORT_DELETED", session.user, detail=f"{key} {kind}")
        return Response(status_code=204)

