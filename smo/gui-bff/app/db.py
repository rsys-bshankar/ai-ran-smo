"""The BFF's own small store: GUI users, the audit log, and the BFF's SME
invoker credential.

Deliberately not in the SMO's shared Postgres schema (migrations/001_init.sql):
none of this is SMO domain data, and the BFF must keep working (login,
audit) even while the SMO stack itself is down. SQLite on a compose volume
by default; any SQLAlchemy URL works via GUI_DATABASE_URL.
"""

import datetime
import uuid
import time

from typing import cast

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Uuid, case, create_engine, delete, event, false, func, inspect, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Base(DeclarativeBase):
    pass


class GuiUser(Base):
    """A local GUI account (table `gui_user`): username, password hash, role, `active`, and the `token_version` that ends every session of the user when bumped.
    `break_glass` marks the one kind of local account that may sign in when `GUI_LOGIN_MODE=oidc`.
    """
    __tablename__ = "gui_user"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Bumped on password change / deactivation: every session JWT carries
    # the version it was issued under, so old sessions stop working at once.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    # PR-SEC-7.7: a local account an admin has flagged as the break-glass login. It signs in with password and one-time code even when GUI_LOGIN_MODE=oidc.
    # Added after the first release of this table: `Database._add_missing_columns` adds it to a database made before (default false, so the previous
    # release's code keeps working on it).
    break_glass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # GUI-5: the user's region/tenant scope claim (app/scoping.py; compact JSON, NULL: unscoped), sent with every call the BFF makes for the user. Added by
    # `_add_missing_columns` to a database made before; NULL keeps every existing user unscoped, as before.
    scope: Mapped[str | None] = mapped_column(String)


class GuiUserTotp(Base):
    """A local user's one-time-code secret (PR-SEC-7.1). `secret_enc` is AES-GCM ciphertext (app/totp.py), never the secret. A row is `confirmed` only
    after the user typed one valid code from it; an unconfirmed row is an enrolment in progress and is never used to sign in. `last_step` is the
    time step of the last code accepted (replay protection: a code is good once), kept here so every instance sharing the database agrees."""
    __tablename__ = "gui_user_totp"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    secret_enc: Mapped[str] = mapped_column(String, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_step: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class GuiRecoveryCode(Base):
    """One of a user's recovery codes (PR-SEC-7.3): only a keyed hash is stored, and `used_at` is set the one time it is spent."""
    __tablename__ = "gui_recovery_code"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class LoginChallenge(Base):
    """The second step of a sign-in (PR-SEC-7.2): written when the password was right, deleted when the code was right, so it works once. The signed
    token the browser holds names `jti`; the row says it is still unspent. Rows past `expires_at` are removed whenever a new one is written."""
    __tablename__ = "gui_login_challenge"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)   # unix seconds


class AuditEntry(Base):
    """Append-only: nothing in the BFF updates or deletes a row, and the
    ORM guard below refuses it if anything ever tries.

    The index on (username, id) serves a user's own sign-ins (`GET /api/me/sign-ins`) and the last-active time per user (`GET /api/admin/users`);
    a database made before it existed gets it from `Database._add_missing_indexes`.
    """
    __tablename__ = "gui_audit_log"
    __table_args__ = (Index("ix_gui_audit_log_username_id", "username", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    username: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)       # PROXY / DENIED / LOGIN / LOGIN_FAILED / USER_* ...
    method: Mapped[str | None] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(String)
    status_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String)


@event.listens_for(Session, "before_flush")
def _audit_log_is_append_only(session, flush_context, instances):
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, AuditEntry):
            raise PermissionError("gui_audit_log is append-only")


class SmoCredential(Base):
    """The BFF's own CAPIF invoker identity at SME (one row). Kept so a BFF
    restart reuses its invoker instead of onboarding a new one each boot.
    The onboarding secret is stored as issued: the BFF has to present it to
    SME's token endpoint, so it can't be hashed here the way SME hashes it.
    """
    __tablename__ = "gui_smo_credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_invoker_id: Mapped[str] = mapped_column(String, nullable=False)
    onboarding_secret: Mapped[str] = mapped_column(String, nullable=False)


class GuiSetting(Base):
    """A value every instance of this database must agree on (PR-ST-5), kept in the database rather than
    in a process: today the session signing key when GUI_JWT_SECRET is not set."""
    __tablename__ = "gui_setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class LoginFailure(Base):
    """Failed-login counter per username (PR-ST-5), so every instance sharing this database counts the same
    failures. Keyed by the name as typed, not by user row: an unknown name is counted and locked exactly like
    a real one, so the lockout does not reveal which usernames exist."""
    __tablename__ = "gui_login_failure"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_failed_at: Mapped[float] = mapped_column(Float, nullable=False)   # unix seconds: start of the counting window


class RevokedSession(Base):
    """A session its owner ended with `POST /api/logout`: its token id (`jti`) until the token would have expired anyway. Shared by every instance, so a
    copied cookie or token stops working on all of them at once, not at the end of its time to live."""
    __tablename__ = "gui_revoked_session"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)   # unix seconds: the token's `exp`; the row is useless after it


class OidcLogin(Base):
    """One OIDC sign-in in flight (PR-SEC-6): written by `GET /api/oidc/login`, consumed (deleted) by `GET /api/oidc/callback`. In the database,
    not in a process, because the callback can land on another instance than the one that sent the browser to the identity provider. `state`
    is the lookup key the provider hands back; `nonce` and `verifier` (the PKCE code verifier) are what the ID token and the code exchange
    are checked against; `binding_hash` is the SHA-256 of a random value held in a cookie of the browser that started the sign-in, so a
    callback URL planted in another browser (login CSRF) does not match. Short-lived: `expires_at` is a few minutes ahead."""
    __tablename__ = "gui_oidc_login"

    state: Mapped[str] = mapped_column(String, primary_key=True)
    nonce: Mapped[str] = mapped_column(String, nullable=False)
    verifier: Mapped[str] = mapped_column(String, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)   # unix seconds


class RappPin(Base):
    """A rApp instance a user pinned to the sidebar (PR-GUI-8, GUI-8.5): at most `MAX_PINS` per user, kept here and not in the browser, so the sidebar is the same
    on every device. A new table, made by `create_all` (nothing to add to `_add_missing_columns`). `instance_id` is a bare reference: the instance lives in rApp
    Management, and a pin of one that no longer exists is dropped the next time the pins are read."""
    __tablename__ = "gui_rapp_pin"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    pinned_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MAX_PINS = 5


class UserPreference(Base):
    """A user's console preferences (theme, text size, accent, start page, rows per page, time zone, clock, motion, alarm sound), one JSON object per
    user (the GUI redesign, BRIEF §4d; validated by app/preferences.py before it is stored). Kept here and not only in the browser, so the console looks
    the same on every device; the browser keeps a copy just so its first paint is right. A new table, made by `create_all`. An SSO user gets a row the
    first time they save; until then `GET /api/me/preferences` answers the defaults."""
    __tablename__ = "gui_user_preference"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)          # JSON text
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class KpiLayout(Base):
    """A KPI dashboard layout a user saved on the KPIs page (GUI-4.3): which KPIs are charted, over what range and place, under a name of the user's,
    at most `MAX_KPI_LAYOUTS` per user. `value` is JSON text validated by app/kpi_layouts.py before it is stored. Kept here so a layout follows the
    user to any browser; a new table, made by `create_all`."""
    __tablename__ = "gui_kpi_layout"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)          # JSON text
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MAX_KPI_LAYOUTS = 20


class ExportJob(Base):
    """An asynchronous CSV export (GUI-9.5b, app/exports.py): who asked (`username`), what (`kind` `decisions` or `audit`, and the filters in
    `params`), where it is (`state` QUEUED, RUNNING, DONE, FAILED; EXPIRED is shown for a job past `expires_at` and such rows are purged on the
    next create), how much it wrote (`rows`, `bytes`) and why it failed (`error`). In the database, not in a process, so every instance on a
    shared GUI_DATABASE_URL lists it and serves its file. `runner_id` is the process that runs it and `heartbeat_at` the last time that process
    showed it was alive: a QUEUED or RUNNING job whose heartbeat is stale is an orphan (its instance stopped) and is marked FAILED "interrupted"
    when read. A new table, made by `create_all`."""
    __tablename__ = "gui_export_job"
    __table_args__ = (Index("ix_gui_export_job_username_created", "username", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="QUEUED")
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    runner_id: Mapped[str | None] = mapped_column(String)
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class ExportChunk(Base):
    """One piece of an export's CSV file, in order (`seq` from 0), at most 1 MiB (`exports.CHUNK_BYTES`). The file is the chunks concatenated;
    a chunk may end inside a row. Deleted with its job."""
    __tablename__ = "gui_export_chunk"

    job_id: Mapped[object] = mapped_column(Uuid, ForeignKey("gui_export_job.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class Database:
    """The BFF's own store (SQLAlchemy over SQLite by default, any SQLAlchemy URL in production), shared by every instance of the BFF.
    Every method opens its own short session and commits before it returns; none leaves a transaction open. State that several instances must agree on (failed-login counts,
    revoked sessions, OIDC sign-ins in flight, one-time-code steps, pins) is kept here, not in memory, and the multi-row races are settled with a conditional
    UPDATE or DELETE and its row count, or with a primary-key insert that may lose to another instance.
    """
    def __init__(self, url: str):
        """Builds the engine and session factory for `url`, creates the missing tables and adds the missing columns.
        SQLite gets `check_same_thread=False` (FastAPI runs routes on a thread pool) and, for an in-memory URL, a `StaticPool` so every session sees the one in-memory database.
        `create_all` is retried because instances starting together on one shared database race on check-then-create; a persistent failure on the last attempt is raised.
        """
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, future=True)
        # Several instances starting on one shared database each run this: check-then-create is not atomic,
        # so a loser of the race can see "already exists". It has only lost, so look again (PR-ST-5).
        for attempt in range(4):
            try:
                Base.metadata.create_all(self.engine)
                break
            except (OperationalError, ProgrammingError):
                if attempt == 3:
                    raise
                time.sleep(0.25)
        self._add_missing_columns()
        self._add_missing_indexes()

    def _add_missing_columns(self) -> None:
        """`create_all` makes absent tables but never alters a table that exists, so a column added to one later is added here (expand only: nullable or
        with a default, so the previous release's code keeps working on the upgraded database). The BFF's tables are not in the Alembic history."""
        wanted = {"gui_user": [("break_glass", "BOOLEAN NOT NULL DEFAULT " + ("false" if self.engine.dialect.name == "postgresql" else "0")),
                               ("scope", "VARCHAR")]}
        for table, columns in wanted.items():
            for name, ddl in columns:
                if name in {c["name"] for c in inspect(self.engine).get_columns(table)}:
                    continue
                try:
                    with self.engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                except (OperationalError, ProgrammingError):
                    # another instance added it first: fine if it is there now, an error if not
                    if name not in {c["name"] for c in inspect(self.engine).get_columns(table)}:
                        raise

    def _add_missing_indexes(self) -> None:
        """Like `_add_missing_columns`, for indexes added to a table after its first release: `create_all` makes an index only with its table.
        `IF NOT EXISTS` works on SQLite and Postgres; a race between two instances starting together is tolerated like a column's."""
        wanted = {"ix_gui_audit_log_username_id": "gui_audit_log (username, id)"}
        for name, target in wanted.items():
            try:
                with self.engine.begin() as conn:
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
            except (OperationalError, ProgrammingError, IntegrityError):
                # another instance made it at the same moment (Postgres can report that as a unique violation): fine if it is there now
                if name not in {i["name"] for i in inspect(self.engine).get_indexes(target.split(" ")[0])}:
                    raise

    def session(self) -> Session:
        return self.sessions()

    # ------------------------------------------------------------ what the audit log says about users (GUI-9.8)

    def user_activity(self, sign_in_actions: tuple[str, ...], username: str | None = None) -> dict[str, tuple[datetime.datetime | None, datetime.datetime | None]]:
        """username -> (time of the user's newest audit row, time of the newest row whose action is one of `sign_in_actions`), in one grouped query;
        only for `username` when given. A user with no audit row is absent."""
        stmt = (select(AuditEntry.username, func.max(AuditEntry.at), func.max(case((AuditEntry.action.in_(sign_in_actions), AuditEntry.at))))
                .where(AuditEntry.username.is_not(None)).group_by(AuditEntry.username))
        if username is not None:
            stmt = stmt.where(AuditEntry.username == username)
        with self.session() as s:
            return {row[0]: (row[1], row[2]) for row in s.execute(stmt)}

    # ------------------------------------------------------------ state shared by every instance (PR-ST-5)

    def shared_setting(self, key: str, default: str) -> str:
        """The stored value of `key`; the first caller to find none stores `default`, and every other
        instance (or a restart) gets that same value, whichever of them asked first."""
        for _ in range(2):
            with self.session() as s:
                row = s.get(GuiSetting, key)
                if row is not None:
                    return row.value
                s.add(GuiSetting(key=key, value=default))
                try:
                    s.commit()
                    return default
                except IntegrityError:   # another instance stored its value first: read that one
                    s.rollback()
        raise RuntimeError(f"could not store or read the setting {key!r}")

    def login_locked(self, username: str, max_failures: int, window_seconds: float, now: float) -> bool:
        """True when `username` has `max_failures` or more failures counted within the last `window_seconds` (all times are unix seconds, `now` supplied by the caller).
        Unknown names are counted like real ones, so the answer never reveals whether the account exists. Read only.
        """
        with self.session() as s:
            row = s.get(LoginFailure, username)
            return row is not None and row.count >= max_failures and now - row.first_failed_at < window_seconds

    def record_login_failure(self, username: str, window_seconds: float, now: float) -> None:
        """Count one failure, atomically in SQL so concurrent instances never lose one: a counter whose window
        has passed restarts at 1, a live one is incremented, and a name with none gets a first row."""
        for _ in range(3):
            with self.session() as s:
                restarted = cast(CursorResult, s.execute(update(LoginFailure).where(
                    LoginFailure.username == username, LoginFailure.first_failed_at <= now - window_seconds)
                    .values(count=1, first_failed_at=now)))
                if restarted.rowcount == 1:
                    s.commit()
                    return
                counted = cast(CursorResult, s.execute(update(LoginFailure).where(
                    LoginFailure.username == username, LoginFailure.first_failed_at > now - window_seconds)
                    .values(count=LoginFailure.count + 1)))
                if counted.rowcount == 1:
                    s.commit()
                    return
                s.add(LoginFailure(username=username, count=1, first_failed_at=now))
                try:
                    s.commit()
                    return
                except IntegrityError:   # another instance added the first row at the same moment: count into it
                    s.rollback()
        raise RuntimeError(f"could not record the failed login for {username!r}")

    def revoke_session(self, jti: str, expires_at: float, now: float) -> None:
        """Record the end of one session (idempotent), and forget the ones whose tokens have expired by themselves."""
        with self.session() as s:
            s.execute(delete(RevokedSession).where(RevokedSession.expires_at <= now))
            if s.get(RevokedSession, jti) is None:
                s.add(RevokedSession(jti=jti, expires_at=expires_at))
            try:
                s.commit()
            except IntegrityError:    # another instance recorded the same logout at the same moment
                s.rollback()

    def session_revoked(self, jti: str) -> bool:
        """True when the session token id `jti` was ended by a logout (and its row has not yet been pruned). Read only; a token still holds `jti` until it expires.
        """
        with self.session() as s:
            return s.get(RevokedSession, jti) is not None

    # ------------------------------------------------------------ OIDC sign-ins in flight (PR-SEC-6)

    def start_oidc_login(self, state: str, nonce: str, verifier: str, binding_hash: str, now: float, ttl_seconds: float, max_pending: int) -> bool:
        """Store one sign-in in flight and forget the ones that have expired. False when `max_pending` are already waiting (the route is
        unauthenticated, so the table must not grow without bound)."""
        with self.session() as s:
            s.execute(delete(OidcLogin).where(OidcLogin.expires_at <= now))
            if (s.scalar(select(func.count()).select_from(OidcLogin)) or 0) >= max_pending:
                s.commit()
                return False
            s.add(OidcLogin(state=state, nonce=nonce, verifier=verifier, binding_hash=binding_hash, expires_at=now + ttl_seconds))
            s.commit()
            return True

    def consume_oidc_login(self, state: str, now: float) -> OidcLogin | None:
        """The sign-in in flight under `state`, removed so it works once: a replayed callback finds nothing. Of two instances asked at the same
        moment only the one whose DELETE removes the row gets it. None for an unknown, used or expired state."""
        with self.session() as s:
            row = s.get(OidcLogin, state)
            if row is None:
                return None
            removed = cast(CursorResult, s.execute(delete(OidcLogin).where(OidcLogin.state == state)))
            s.commit()
            if removed.rowcount != 1 or row.expires_at <= now:
                return None
            return row

    # ------------------------------------------------------------ one-time codes (PR-SEC-7)

    def totp_state(self, username: str) -> tuple[bool, bool]:
        """(enrolled, pending): a confirmed secret, or one waiting for its first code."""
        with self.session() as s:
            row = s.get(GuiUserTotp, username)
            return (row is not None and row.confirmed, row is not None and not row.confirmed)

    def totp_enrolled_users(self) -> set[str]:
        """The usernames with a confirmed one-time-code secret (an enrolment still waiting for its first code does not count). Read only.
        """
        with self.session() as s:
            return set(s.scalars(select(GuiUserTotp.username).where(GuiUserTotp.confirmed.is_(True))).all())

    def begin_totp(self, username: str, secret_enc: str) -> bool:
        """Store a new, unconfirmed secret (replacing an earlier unconfirmed one). False when the user already has a confirmed one."""
        for _ in range(2):
            with self.session() as s:
                row = s.get(GuiUserTotp, username)
                if row is not None and row.confirmed:
                    return False
                if row is None:
                    s.add(GuiUserTotp(username=username, secret_enc=secret_enc, confirmed=False))
                else:
                    row.secret_enc, row.created_at = secret_enc, _now()
                try:
                    s.commit()
                    return True
                except IntegrityError:      # another instance began an enrolment for the same user at the same moment: update its row
                    s.rollback()
        raise RuntimeError(f"could not store the one-time-code secret for {username!r}")

    def totp_secret(self, username: str) -> tuple[str, bool, int | None] | None:
        """(encrypted secret, confirmed, last step) of the user's row."""
        with self.session() as s:
            row = s.get(GuiUserTotp, username)
            return None if row is None else (row.secret_enc, row.confirmed, row.last_step)

    def confirm_totp(self, username: str, step: int, recovery_hashes: list[str]) -> bool:
        """Make the pending secret live, remember the step of the code that proved it (it cannot be used again to sign in) and store the recovery codes,
        in one transaction. False when there is nothing pending (another request confirmed it first)."""
        with self.session() as s:
            done = cast(CursorResult, s.execute(update(GuiUserTotp).where(GuiUserTotp.username == username, GuiUserTotp.confirmed.is_(False))
                                                .values(confirmed=True, last_step=step, confirmed_at=_now())))
            if done.rowcount != 1:
                s.rollback()
                return False
            s.execute(delete(GuiRecoveryCode).where(GuiRecoveryCode.username == username))
            s.add_all(GuiRecoveryCode(username=username, code_hash=h) for h in recovery_hashes)
            s.commit()
            return True

    def use_totp_step(self, username: str, step: int) -> bool:
        """Accept the code of time step `step` once: true only for the request whose UPDATE moves the stored step forward, so a replay, or the same
        code sent to two instances at once, fails."""
        with self.session() as s:
            done = cast(CursorResult, s.execute(update(GuiUserTotp).where(
                GuiUserTotp.username == username, GuiUserTotp.confirmed.is_(True), (GuiUserTotp.last_step.is_(None)) | (GuiUserTotp.last_step < step))
                .values(last_step=step)))
            s.commit()
            return done.rowcount == 1

    def use_recovery_code(self, username: str, code_hash: str) -> int | None:
        """Spend a recovery code: the number left afterwards, or None when no unused code matches."""
        with self.session() as s:
            done = cast(CursorResult, s.execute(update(GuiRecoveryCode).where(
                GuiRecoveryCode.username == username, GuiRecoveryCode.code_hash == code_hash, GuiRecoveryCode.used_at.is_(None)).values(used_at=_now())))
            s.commit()
            if done.rowcount != 1:
                return None
        return self.recovery_codes_left(username)

    def recovery_code_slots(self, username: str) -> list[tuple[int, datetime.datetime | None]]:
        """(slot, when it was used or None) for each of the user's recovery codes, slot 1 the first issued (the order they were shown in). Never a code
        or its hash."""
        with self.session() as s:
            used = s.scalars(select(GuiRecoveryCode.used_at).where(GuiRecoveryCode.username == username).order_by(GuiRecoveryCode.id)).all()
        return [(i + 1, at) for i, at in enumerate(used)]

    def recovery_codes_left(self, username: str) -> int:
        """The number of unused recovery codes of `username` (0 when the user has none). Read only."""
        with self.session() as s:
            return s.scalar(select(func.count()).select_from(GuiRecoveryCode).where(GuiRecoveryCode.username == username, GuiRecoveryCode.used_at.is_(None))) or 0

    def replace_recovery_codes(self, username: str, recovery_hashes: list[str]) -> None:
        """Deletes all of the user's recovery codes and stores `recovery_hashes` (keyed hashes, never the codes) in their place, in one transaction.
        """
        with self.session() as s:
            s.execute(delete(GuiRecoveryCode).where(GuiRecoveryCode.username == username))
            s.add_all(GuiRecoveryCode(username=username, code_hash=h) for h in recovery_hashes)
            s.commit()

    def reset_totp(self, username: str) -> bool:
        """Remove the user's one-time-code secret, recovery codes and open challenges (an admin's reset, or the user's deletion). True when there was a secret."""
        with self.session() as s:
            removed = cast(CursorResult, s.execute(delete(GuiUserTotp).where(GuiUserTotp.username == username)))
            s.execute(delete(GuiRecoveryCode).where(GuiRecoveryCode.username == username))
            s.execute(delete(LoginChallenge).where(LoginChallenge.username == username))
            s.commit()
            return removed.rowcount > 0

    def create_challenge(self, jti: str, username: str, expires_at: float, now: float) -> None:
        """Records the second step of a sign-in: the challenge `jti` for `username`, valid until `expires_at` (unix seconds).
        Challenges already past `now` are deleted in the same transaction, which is the only place they are cleaned up.
        """
        with self.session() as s:
            s.execute(delete(LoginChallenge).where(LoginChallenge.expires_at <= now))
            s.add(LoginChallenge(jti=jti, username=username, expires_at=expires_at))
            s.commit()

    def challenge_pending(self, jti: str, username: str, now: float) -> bool:
        """True when challenge `jti` exists, belongs to `username` and has not expired. Read only: it does not spend the challenge (`consume_challenge` does).
        """
        with self.session() as s:
            row = s.get(LoginChallenge, jti)
            return row is not None and row.username == username and row.expires_at > now

    def consume_challenge(self, jti: str, username: str, now: float) -> bool:
        """Spend the challenge: of two requests at once only the one whose DELETE removes the row gets true."""
        with self.session() as s:
            done = cast(CursorResult, s.execute(delete(LoginChallenge).where(
                LoginChallenge.jti == jti, LoginChallenge.username == username, LoginChallenge.expires_at > now)))
            s.commit()
            return done.rowcount == 1

    def clear_login_failures(self, username: str) -> None:
        """Forgets the failed-login counter of `username` (after a successful sign-in or an admin's unlock); a no-op when there is none.
        """
        with self.session() as s:
            s.execute(delete(LoginFailure).where(LoginFailure.username == username))
            s.commit()

    def store_smo_credential(self, api_invoker_id: str, secret: str, stale_invoker_id: str | None = None) -> bool:
        """Store the BFF's SME invoker identity. With no `stale_invoker_id` this is a first registration: it
        stores only if none exists. With one, it replaces that identity only if it is still the stored one
        (compare-and-swap). False means another instance got there first; use `SmoCredential` as stored."""
        with self.session() as s:
            if stale_invoker_id is None:
                s.add(SmoCredential(id=1, api_invoker_id=api_invoker_id, onboarding_secret=secret))
                try:
                    s.commit()
                    return True
                except IntegrityError:
                    s.rollback()
                    return False
            done = cast(CursorResult, s.execute(update(SmoCredential).where(SmoCredential.id == 1, SmoCredential.api_invoker_id == stale_invoker_id)
                                                .values(api_invoker_id=api_invoker_id, onboarding_secret=secret)))
            s.commit()
            return done.rowcount == 1

    # ------------------------------------------------------------ pinned rApps (PR-GUI-8, GUI-8.5)

    def pins(self, username: str) -> list[str]:
        """The instance ids the user pinned, oldest first."""
        with self.session() as s:
            return list(s.scalars(select(RappPin.instance_id).where(RappPin.username == username).order_by(RappPin.pinned_at, RappPin.instance_id)))

    def add_pin(self, username: str, instance_id: str) -> str:
        """`added`, `exists` (pinning again is fine) or `full` (the user already has `MAX_PINS`). Of two pins at once that would pass the limit, the one
        that finds itself over it takes its row back."""
        with self.session() as s:
            if s.get(RappPin, (username, instance_id)) is not None:
                return "exists"
            if cast(int, s.scalar(select(func.count()).select_from(RappPin).where(RappPin.username == username))) >= MAX_PINS:
                return "full"
            s.add(RappPin(username=username, instance_id=instance_id))
            try:
                s.commit()
            except IntegrityError:           # the same pin from another request a moment earlier
                s.rollback()
                return "exists"
            if cast(int, s.scalar(select(func.count()).select_from(RappPin).where(RappPin.username == username))) > MAX_PINS:
                s.execute(delete(RappPin).where(RappPin.username == username, RappPin.instance_id == instance_id))
                s.commit()
                return "full"
            return "added"

    def remove_pin(self, username: str, instance_id: str) -> None:
        """Unpins `instance_id` for `username`; a no-op when it was not pinned."""
        with self.session() as s:
            s.execute(delete(RappPin).where(RappPin.username == username, RappPin.instance_id == instance_id))
            s.commit()

    def remove_all_pins(self, username: str) -> None:
        """Drops every pin of `username` (used when the account is deleted)."""
        with self.session() as s:
            s.execute(delete(RappPin).where(RappPin.username == username))
            s.commit()

    # ------------------------------------------------------------ console preferences (GUI redesign, BRIEF §4d)

    def preferences(self, username: str) -> str | None:
        """The user's stored preferences as JSON text, or None when they never saved any."""
        with self.session() as s:
            row = s.get(UserPreference, username)
            return row.value if row is not None else None

    def save_preferences(self, username: str, value: str) -> None:
        """Store (insert or replace) the user's preferences, JSON text already validated by the caller. Two saves at once: the later commit wins."""
        with self.session() as s:
            row = s.get(UserPreference, username)
            if row is None:
                s.add(UserPreference(username=username, value=value))
            else:
                row.value, row.updated_at = value, _now()
            try:
                s.commit()
            except IntegrityError:          # the same user's first save from another request a moment earlier: replace it
                s.rollback()
                s.execute(update(UserPreference).where(UserPreference.username == username).values(value=value, updated_at=_now()))
                s.commit()

    def remove_preferences(self, username: str) -> None:
        """Forget the user's preferences (they are back on the defaults); used when the user is deleted."""
        with self.session() as s:
            s.execute(delete(UserPreference).where(UserPreference.username == username))
            s.commit()

    # ------------------------------------------------------------ saved KPI dashboard layouts (GUI-4.3)

    def kpi_layouts(self, username: str) -> list[tuple[str, str, datetime.datetime]]:
        """(name, JSON value, updated at) of each layout the user saved, by name."""
        with self.session() as s:
            rows = s.execute(select(KpiLayout.name, KpiLayout.value, KpiLayout.updated_at).where(KpiLayout.username == username).order_by(KpiLayout.name))
            return [(r.name, r.value, r.updated_at) for r in rows]

    def save_kpi_layout(self, username: str, name: str, value: str) -> str:
        """`saved` (a new layout, or one of that name replaced) or `full` (a new name when the user already has `MAX_KPI_LAYOUTS`). Of two new
        layouts at once that would pass the limit, the one that finds itself over it takes its row back."""
        with self.session() as s:
            row = s.get(KpiLayout, (username, name))
            if row is not None:
                row.value, row.updated_at = value, _now()
                s.commit()
                return "saved"
            if cast(int, s.scalar(select(func.count()).select_from(KpiLayout).where(KpiLayout.username == username))) >= MAX_KPI_LAYOUTS:
                return "full"
            s.add(KpiLayout(username=username, name=name, value=value))
            try:
                s.commit()
            except IntegrityError:          # the same name saved by another request a moment earlier: replace it
                s.rollback()
                s.execute(update(KpiLayout).where(KpiLayout.username == username, KpiLayout.name == name).values(value=value, updated_at=_now()))
                s.commit()
                return "saved"
            if cast(int, s.scalar(select(func.count()).select_from(KpiLayout).where(KpiLayout.username == username))) > MAX_KPI_LAYOUTS:
                s.execute(delete(KpiLayout).where(KpiLayout.username == username, KpiLayout.name == name))
                s.commit()
                return "full"
            return "saved"

    def remove_kpi_layout(self, username: str, name: str | None = None) -> None:
        """Deletes the user's layout `name`, or every layout of the user when `name` is None (the account is deleted); a no-op when there is none."""
        with self.session() as s:
            stmt = delete(KpiLayout).where(KpiLayout.username == username)
            s.execute(stmt if name is None else stmt.where(KpiLayout.name == name))
            s.commit()
