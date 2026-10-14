"""Automatic rotation of the secret of running rApp instances (SEC-15.19), run by the rApp Management worker (`tasks.py`, `python -m smo_shared.worker`).

An instance's secret used to change only when a person pressed **Re-send credentials** (`POST /instances/{id}/credentials/renew`). With
`RAPP_MGMT_SECRET_ROTATION_DAYS` above 0 the worker does the same renewal by itself for every RUNNING instance whose secret is older than that many days. The default
is 0: nothing is rotated and nothing is read or written.

Which instances: RUNNING, with an identity (`oauth_client_id`) and a registered operator API (the way the new secret reaches the workload), no upgrade pending, and
`coalesce(credentials_issued_at, created_at)` at least the interval ago, oldest first. At most `RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN` (default 10) per run, so a
fleet whose secrets all fell due together is rotated over several runs, not at once. An instance without an operator API cannot be handed a secret by the platform
and is never rotated here (it is rotated by hand). With `RAPP_CREDENTIAL_DELIVERY=kubernetes` nothing is rotated here at all (see `rotate_due`).

One instance, in this order, so a workload that cannot take the secret is never put out of step:

  1. the workload is probed through the gateway (`GET /rapps/{id}/operator/...`): any answer from the rApp counts as reachable, the gateway's 502/503/504 and its
     "operator API not registered" 404 do not. Unreachable: nothing is changed at SME, the failure is recorded and the instance is tried again at the next run;
  2. SME issues a new secret (`issue_secret`); from that moment the old secret is refused and the identity's access tokens are revoked (SME holds ONE secret per
     identity, so there is no grace period: the short window is the time between this call and the push, and a token the workload already holds is cut off with it);
  3. the secret is pushed to the workload (`push_credentials`), up to `PUSH_ATTEMPTS` times. Taken: `credentials_issued_at` moves to now, an audit row and an event
     are written and the instance is not due again for the interval. Not taken: the failure is recorded (the old secret is already invalid, the audit row and event
     say so), the timestamp stays, and the next run rotates again; **Re-send credentials** by hand does the same at once.

Exactly once: the worker's task is claimed by one replica per interval (`smo_shared.single_runner`, a database row and an advisory lock), each instance is read again
just before it is rotated, and the new timestamp makes it not due.

No secret is logged, audited, published or returned: the audit detail and the event carry the instance id, the identity id and a reason code only.
"""

import datetime
import logging
import os
import time
import uuid
from collections.abc import Callable

import httpx
from prometheus_client import Counter
from sqlalchemy import func, select, update

from smo_shared import audit, credential_delivery, events, roles
from smo_shared import db as shared_db
from smo_shared.invoker import on_own_account
from smo_shared.r1_client import R1Client

from .models import RAppInstance
from .provisioning import issue_secret, push_credentials
from .statemachine import InstanceState

log = logging.getLogger("rapp-mgmt.rotation")

ACTOR = "system:secret-rotation"        # who the audit row names: the worker, not a person and not an rApp
SOURCE = "/smo/rapp-mgmt"
DEFAULT_MAX_PER_RUN = 10
PUSH_ATTEMPTS = 3                       # a push the workload did not take is repeated this many times before the rotation is given up for this run
PUSH_PAUSE_SECONDS = 2.0                # between those attempts

ROTATIONS = Counter("smo_rapp_credential_rotations_total",
                    "Automatic rotations of an rApp instance's secret by the rApp Management worker (SEC-15.19), by outcome: rotated, unreachable (the workload could not be "
                    "reached, nothing changed), sme_refused (SME issued no new secret, nothing changed), push_failed (SME issued it but the workload did not take it) or skipped "
                    "(no longer due when its turn came).", ["outcome"])


def rotation_days() -> int:
    """`RAPP_MGMT_SECRET_ROTATION_DAYS` as whole days; 0 (the default, unset, not a number or negative) means the automatic rotation is off. Read on every run."""
    try:
        return max(0, int(os.environ.get("RAPP_MGMT_SECRET_ROTATION_DAYS", "0")))
    except ValueError:
        return 0


def max_per_run() -> int:
    """`RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN`: the most instances one run rotates (default 10, at least 1; an invalid value gives the default). Read on every run."""
    try:
        return max(1, int(os.environ.get("RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN", DEFAULT_MAX_PER_RUN)))
    except ValueError:
        return DEFAULT_MAX_PER_RUN


def _issued():
    """The column expression for when an instance's secret was issued: the stamp, or the creation time for a row from before the stamp existed."""
    return func.coalesce(RAppInstance.credentials_issued_at, RAppInstance.created_at)


def _due_condition(cutoff: datetime.datetime):
    """The SQL condition for "this instance is to be rotated": see the module description for which instances."""
    return (RAppInstance.state == InstanceState.RUNNING.value, RAppInstance.oauth_client_id.is_not(None), RAppInstance.operator_api_base.is_not(None),
            RAppInstance.pending_upgrade_instance_id.is_(None), _issued() <= cutoff)


def workload_reachable(inst: RAppInstance) -> bool:
    """True when the rApp serving the instance answers through the gateway. The probe is a GET of the route the push uses (a POST-only route): the rApp's own answer,
    whatever its status (405 from the SDK), shows it is up; the gateway's 502/503/504 or its own 404 `OPERATOR_API_NOT_REGISTERED` show it is not."""
    try:
        resp = R1Client().get(f"/rapps/{inst.instance_id}/operator/instances/{inst.instance_id}/credentials")
    except httpx.HTTPError:
        return False
    if resp.status_code in (502, 503, 504):
        return False
    if resp.status_code == 404:
        try:
            return resp.json().get("title") != "OPERATOR_API_NOT_REGISTERED"
        except ValueError:
            return True
    return True


def _push(inst: RAppInstance, secret: str, pause: Callable[[float], None]) -> bool:
    """Hands the new secret to the workload, up to `PUSH_ATTEMPTS` times with a pause between; True when the workload took it."""
    for attempt in range(PUSH_ATTEMPTS):
        if attempt:
            pause(PUSH_PAUSE_SECONDS)
        if push_credentials(inst, secret):
            return True
    return False


def _record(db, inst: RAppInstance, result: str, change: str, reason: str | None, *, old_secret_invalid: bool = False, days: int, now: datetime.datetime) -> None:
    """Writes the audit row and the event of one attempt in the caller's transaction (the caller commits). Neither carries a secret."""
    detail: dict = {"instanceId": str(inst.instance_id), "oauthClientId": inst.oauth_client_id, "olderThanDays": days}
    if reason:
        detail["reason"] = reason
        detail["oldSecretStillValid"] = not old_secret_invalid
    audit.record(db, actor=ACTOR, role=roles.ROLE_INTERNAL, action="ROTATE_CREDENTIALS", target=f"/rapp-mgmt/instances/{inst.instance_id}/credentials",
                 result=result, detail=detail, now=now)
    events.publish(db, "rapp-instance", change, SOURCE, str(inst.instance_id), detail)


def rotate_instance(session_factory, instance_id: uuid.UUID, days: int, now: datetime.datetime, pause: Callable[[float], None] = time.sleep) -> str:
    """Rotates one instance if it is still due; returns the outcome (the label of `ROTATIONS`). Its own session and transaction."""
    with session_factory() as db:
        due = db.scalar(select(RAppInstance.instance_id).where(RAppInstance.instance_id == instance_id, *_due_condition(now - datetime.timedelta(days=days))))
        inst = db.get(RAppInstance, instance_id) if due else None
        if inst is None:
            return "skipped"        # renewed by hand, upgraded, terminated or deleted since the list was made
        with on_own_account():
            if not workload_reachable(inst):
                outcome, reason, invalid = "unreachable", "WORKLOAD_UNREACHABLE", False
            elif (secret := issue_secret(inst)) is None:
                outcome, reason, invalid = "sme_refused", "SME_DID_NOT_ISSUE_A_SECRET", False
            elif not _push(inst, secret, pause):
                outcome, reason, invalid = "push_failed", "WORKLOAD_DID_NOT_TAKE_THE_SECRET", True
            else:
                outcome, reason, invalid = "rotated", None, False
        if reason is None:
            db.execute(update(RAppInstance).where(RAppInstance.instance_id == instance_id).values(credentials_issued_at=now))   # not the ORM: no version bump, nothing else on the row changed
            _record(db, inst, "ok", "credentials-rotated", None, days=days, now=now)
        else:
            _record(db, inst, "failed", "credentials-rotation-failed", reason, old_secret_invalid=invalid, days=days, now=now)
            log.warning("automatic rotation of an instance's secret failed", extra={"instance_id": str(instance_id), "reason": reason})
        db.commit()
    return outcome


def rotate_due(session_factory=None, now: datetime.datetime | None = None, pause: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """One run of the automatic rotation: rotates up to `max_per_run()` due instances, oldest first, and returns how many ended in each outcome (empty when the rotation
    is off, `RAPP_MGMT_SECRET_ROTATION_DAYS` 0, or nothing was due). An instance that fails is not tried again within the run."""
    days = rotation_days()
    if days == 0:
        return {}
    if credential_delivery.mode() != "none":
        # The workload takes its credentials from a Kubernetes Secret that only the rApp Management service (the one pod with Kubernetes access) writes: a rotation that
        # pushed the secret to the operator API alone would leave that Secret holding the dead one for the workload's next restart.
        log.warning("automatic secret rotation is off while RAPP_CREDENTIAL_DELIVERY is %s: the credentials Secret of a workload is not rewritten by the worker",
                    credential_delivery.mode())
        return {}
    session_factory = session_factory or shared_db.SessionLocal           # looked up at call time: a test (or the worker) may have set it since the import
    now = now or datetime.datetime.now(datetime.UTC)
    with session_factory() as db:
        ids = list(db.scalars(select(RAppInstance.instance_id).where(*_due_condition(now - datetime.timedelta(days=days)))
                              .order_by(_issued(), RAppInstance.instance_id).limit(max_per_run())))
    done: dict[str, int] = {}
    for instance_id in ids:
        outcome = rotate_instance(session_factory, instance_id, days, now, pause)
        ROTATIONS.labels(outcome).inc()
        done[outcome] = done.get(outcome, 0) + 1
    if done:
        log.info("automatic secret rotation ran", extra={"outcomes": done})
    return done
