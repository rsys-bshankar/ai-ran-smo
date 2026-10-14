"""The automatic rotation of the secret of running rApp instances (SEC-15.19): `app/rotation.py` and the worker task in `app/tasks.py`.

Run: `cd smo/rapp-mgmt && PYTHONPATH=.:../shared python -m pytest tests/test_rotation.py -q`. R1 is a recording fake (SME's secret route, the gateway's operator path); the database is
the SQLite of `test_main.py` with the audit chain, the event feed and the single-runner table added.
"""

import datetime
import json
import uuid

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from smo_shared import audit, events
from smo_shared.audit import AuditEntry, AuditHead
from smo_shared.db import Base
from smo_shared.events import SmoEvent
from smo_shared.single_runner import PeriodicRun
from smo_shared.timeutil import as_utc
from smo_shared.worker import tick

from test_main import FakeR1Response, client, db_session_factory, fake_r1_delete  # noqa: F401  (fixtures)
from test_operator_api import _create

from app import rotation, tasks
from app.models import RAppInstance
from app.provisioning import issue_secret


class FakeR1:
    """What the rotation calls through R1: the gateway's operator path (probe GET, credentials POST) and SME's secret route. Each answer is a field the test sets;
    `calls` records (method, path) in order and `pushed` the bodies pushed to workloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.pushed: list[tuple[str, dict]] = []
        self.probe: object = 405                  # a status, a (status, json) pair, or an exception to raise
        self.sme: object = 200                    # 200 issues a secret; another status refuses; an exception is raised
        self.push: list[int] | int = 204          # a status, or one status per attempt (the last repeats)
        self.issued = 0

    def get(self, _self, path, **kw):
        """The probe of the operator path."""
        self.calls.append(("GET", path))
        if isinstance(self.probe, Exception):
            raise self.probe
        status, body = self.probe if isinstance(self.probe, tuple) else (self.probe, {})
        return FakeR1Response(status, body)

    def post(self, _self, path, json=None, **kw):
        """SME's secret route, or the push to a workload."""
        self.calls.append(("POST", path))
        if path.startswith("/sme/"):
            if isinstance(self.sme, Exception):
                raise self.sme
            if self.sme != 200:
                return FakeR1Response(self.sme, {})
            self.issued += 1
            return FakeR1Response(200, {"apiInvokerId": path.split("/")[3], "onboardingSecret": f"secret-{self.issued}-abc"})
        self.pushed.append((path, json))
        pushes = len([c for c in self.calls if c[0] == "POST" and not c[1].startswith("/sme/")])
        status = self.push if isinstance(self.push, int) else self.push[min(pushes, len(self.push)) - 1]
        return FakeR1Response(status, {})

    def sme_posts(self) -> int:
        """How many times SME was asked for a new secret."""
        return len([c for c in self.calls if c[1].startswith("/sme/")])


@pytest.fixture
def r1(monkeypatch):
    """A `FakeR1` standing in for `R1Client.get` and `.post`."""
    fake = FakeR1()
    monkeypatch.setattr("app.rotation.R1Client.get", lambda self, path, **kw: fake.get(self, path, **kw))
    monkeypatch.setattr("app.rotation.R1Client.post", lambda self, path, json=None, **kw: fake.post(self, path, json=json, **kw))
    return fake


@pytest.fixture
def db(db_session_factory):
    """The rApp Management test database with the tables the rotation also writes: the audit chain, the event feed and the single runner's claims."""
    Base.metadata.create_all(db_session_factory.kw["bind"], tables=[AuditEntry.__table__, AuditHead.__table__, SmoEvent.__table__, PeriodicRun.__table__])
    return db_session_factory


@pytest.fixture(autouse=True)
def quiet_pauses(monkeypatch):
    """No test waits between push attempts."""
    monkeypatch.setattr(rotation, "PUSH_PAUSE_SECONDS", 0.0)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _instance(factory, *, issued_days: float | None = 40, created_days: float = 100, state: str = "RUNNING", base: str | None = "http://rapp:8000",
              client_id: str | None = "", pending: uuid.UUID | None = None) -> uuid.UUID:
    """Adds an instance whose secret was issued `issued_days` ago (None: the column is empty, as on a row from before it) and returns its id."""
    iid = uuid.uuid4()
    with factory() as session:
        session.add(RAppInstance(
            instance_id=iid, package_id=uuid.uuid4(), state=state, oauth_client_id=f"inv-{iid}" if client_id == "" else client_id, operator_api_base=base,
            created_at=_now() - datetime.timedelta(days=created_days), pending_upgrade_instance_id=pending,
            credentials_issued_at=None if issued_days is None else _now() - datetime.timedelta(days=issued_days)))
        session.commit()
    return iid


def _issued_at(factory, iid: uuid.UUID):
    with factory() as session:
        return session.get(RAppInstance, iid).credentials_issued_at


def _outcomes() -> dict[str, float]:
    return {o: REGISTRY.get_sample_value("smo_rapp_credential_rotations_total", {"outcome": o}) or 0.0
            for o in ("rotated", "unreachable", "sme_refused", "push_failed", "skipped")}


def _rows(factory) -> tuple[list[AuditEntry], list[SmoEvent]]:
    with factory() as session:
        return list(session.scalars(select(AuditEntry).order_by(AuditEntry.seq))), list(session.scalars(select(SmoEvent).order_by(SmoEvent.seq)))


def test_the_rotation_is_off_by_default_and_does_nothing(db, r1, monkeypatch):
    """With the setting unset, 0, negative or not a number, a run touches nothing: no call to R1, no row changed, no audit row, no event."""
    iid = _instance(db, issued_days=400)
    before = _issued_at(db, iid)
    for value in (None, "0", "-5", "soon"):
        if value is None:
            monkeypatch.delenv("RAPP_MGMT_SECRET_ROTATION_DAYS", raising=False)
        else:
            monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", value)
        assert rotation.rotation_days() == 0
        assert rotation.rotate_due(db) == {}
    assert r1.calls == [] and _issued_at(db, iid) == before and _rows(db) == ([], [])


def test_a_due_instance_is_rotated_and_one_that_is_not_due_is_left_alone(db, r1, monkeypatch):
    """Over 30 days old and RUNNING: rotated (SME asked, the new secret pushed to the workload, the issue time moved to now). 10 days old: untouched."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    due = _instance(db, issued_days=31)
    young = _instance(db, issued_days=10)
    young_before = _issued_at(db, young)

    assert rotation.rotate_due(db) == {"rotated": 1}

    assert [c for c in r1.calls if c[1].startswith("/sme/")] == [("POST", f"/sme/invoker-registrations/inv-{due}/secret")]
    assert r1.pushed == [(f"/rapps/{due}/operator/instances/{due}/credentials", {"oauthClientId": f"inv-{due}", "oauthClientSecret": "secret-1-abc"})]
    assert _now() - as_utc(_issued_at(db, due)) < datetime.timedelta(minutes=1)
    assert _issued_at(db, young) == young_before
    assert rotation.rotate_due(db) == {}                       # the rotated one is not due again
    assert r1.sme_posts() == 1


def test_a_row_without_an_issue_time_counts_from_its_creation(db, r1, monkeypatch):
    """A row from before the column (NULL) is read as issued at creation: rotated when created long ago, left alone when created recently."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    old = _instance(db, issued_days=None, created_days=90)
    new = _instance(db, issued_days=None, created_days=5)
    assert rotation.rotate_due(db) == {"rotated": 1}
    assert [p for p, _ in r1.pushed] == [f"/rapps/{old}/operator/instances/{old}/credentials"]
    assert _issued_at(db, new) is None


def test_only_running_instances_with_an_operator_api_and_no_pending_upgrade_are_rotated(db, r1, monkeypatch):
    """A DEPLOYING, FAULTED or UNDEPLOYED instance, one with no operator API, one with no identity and one with an upgrade pending are all left alone, however old."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    for state in ("DEPLOYING", "FAULTED", "UNDEPLOYED", "UPGRADING"):
        _instance(db, state=state)
    _instance(db, base=None)
    _instance(db, client_id=None)
    _instance(db, pending=uuid.uuid4())
    assert rotation.rotate_due(db) == {} and r1.calls == []


def test_a_run_rotates_at_most_the_limit_oldest_first_and_the_next_run_the_rest(db, r1, monkeypatch):
    """Five due instances with a limit of two: the two with the oldest secrets in the first run, the next two in the second, the last in the third."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN", "2")
    ids = [_instance(db, issued_days=days) for days in (35, 50, 41, 60, 33)]       # by age: ids[3], ids[1], ids[2], ids[0], ids[4]
    order = [ids[3], ids[1], ids[2], ids[0], ids[4]]

    assert rotation.rotate_due(db) == {"rotated": 2}
    assert [p.split("/")[2] for p, _ in r1.pushed] == [str(order[0]), str(order[1])]
    assert rotation.rotate_due(db) == {"rotated": 2}
    assert rotation.rotate_due(db) == {"rotated": 1}
    assert rotation.rotate_due(db) == {}
    assert [p.split("/")[2] for p, _ in r1.pushed] == [str(i) for i in order]


@pytest.mark.parametrize("value, expected", [(None, 10), ("3", 3), ("0", 1), ("-4", 1), ("many", 10)])
def test_the_limit_defaults_to_ten_and_is_at_least_one(monkeypatch, value, expected):
    """`RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN`: unset or invalid gives 10, anything below 1 gives 1."""
    if value is None:
        monkeypatch.delenv("RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN", raising=False)
    else:
        monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN", value)
    assert rotation.max_per_run() == expected


@pytest.mark.parametrize("probe", [502, 503, 504, (404, {"title": "OPERATOR_API_NOT_REGISTERED"}), httpx.ConnectError("down")])
def test_a_workload_that_cannot_be_reached_is_not_rotated_and_is_tried_again(db, r1, monkeypatch, probe):
    """The gateway cannot reach the workload (502, 503, 504, no operator API registered, or the gateway itself is down): SME is not asked, the old secret stays valid and
    the issue time does not move; an audit row and an event say why, the failure is counted, and the next run rotates it once the workload answers."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    iid = _instance(db)
    before, counts = _issued_at(db, iid), _outcomes()
    r1.probe = probe

    assert rotation.rotate_due(db) == {"unreachable": 1}

    assert r1.sme_posts() == 0 and r1.pushed == [] and _issued_at(db, iid) == before
    assert _outcomes()["unreachable"] == counts["unreachable"] + 1
    audit_rows, event_rows = _rows(db)
    assert [(a.action, a.result, a.actor, a.detail["reason"], a.detail["oldSecretStillValid"]) for a in audit_rows] == [
        ("ROTATE_CREDENTIALS", "failed", "system:secret-rotation", "WORKLOAD_UNREACHABLE", True)]
    assert [e.type for e in event_rows] == ["org.oran.smo.rapp-instance.credentials-rotation-failed"] and event_rows[0].subject == str(iid)

    r1.probe = 405                                             # the workload is back
    assert rotation.rotate_due(db) == {"rotated": 1} and r1.sme_posts() == 1


@pytest.mark.parametrize("probe", [405, 200, 401, 404, (404, {"title": "NOT_FOUND"})])
def test_any_answer_from_the_workload_itself_counts_as_reachable(db, r1, monkeypatch, probe):
    """The rApp's own answer to the probe, whatever its status (the SDK's route is POST-only, so 405; a workload with another SDK may say 404), shows it is up."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    _instance(db)
    r1.probe = probe
    assert rotation.rotate_due(db) == {"rotated": 1}


def test_a_404_that_is_not_json_is_the_workloads_own_answer(db, r1, monkeypatch):
    """A 404 whose body is not JSON did not come from the gateway's problem document: the workload answered, so it is reachable."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    _instance(db)

    class NotJson(FakeR1Response):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr("app.rotation.R1Client.get", lambda self, path, **kw: NotJson(404, None))
    assert rotation.rotate_due(db) == {"rotated": 1}


@pytest.mark.parametrize("sme", [500, 503, 404, httpx.ConnectError("down")])
def test_when_sme_issues_no_secret_nothing_changes_and_nothing_is_pushed(db, r1, monkeypatch, sme):
    """SME refuses or cannot be reached: no push, the issue time stays, the failure is audited (the old secret is still valid) and the instance is due again next run."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    iid = _instance(db)
    before = _issued_at(db, iid)
    r1.sme = sme

    assert rotation.rotate_due(db) == {"sme_refused": 1}

    assert r1.pushed == [] and _issued_at(db, iid) == before
    audit_rows, event_rows = _rows(db)
    assert audit_rows[0].detail["reason"] == "SME_DID_NOT_ISSUE_A_SECRET" and audit_rows[0].detail["oldSecretStillValid"] is True
    assert event_rows[0].type.endswith("credentials-rotation-failed")
    r1.sme = 200
    assert rotation.rotate_due(db) == {"rotated": 1}


def test_a_secret_the_workload_does_not_take_is_retried_then_recorded_and_the_next_run_rotates_again(db, r1, monkeypatch):
    """SME issued the secret (the old one is invalid from then on) but the workload refuses the push: three attempts with a pause between, then a failure that says the old
    secret is no longer valid, the issue time unchanged so the instance is due again; the next run issues and pushes a new secret."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    iid = _instance(db)
    before = _issued_at(db, iid)
    pauses: list[float] = []
    r1.push = 503

    assert rotation.rotate_due(db, pause=pauses.append) == {"push_failed": 1}

    assert len(r1.pushed) == rotation.PUSH_ATTEMPTS == 3 and pauses == [rotation.PUSH_PAUSE_SECONDS] * 2
    assert _issued_at(db, iid) == before
    audit_rows, event_rows = _rows(db)
    assert (audit_rows[0].result, audit_rows[0].detail["reason"], audit_rows[0].detail["oldSecretStillValid"]) == ("failed", "WORKLOAD_DID_NOT_TAKE_THE_SECRET", False)
    assert event_rows[0].type.endswith("credentials-rotation-failed") and event_rows[0].data["oldSecretStillValid"] is False

    r1.push = 204
    assert rotation.rotate_due(db) == {"rotated": 1} and r1.sme_posts() == 2
    assert r1.pushed[-1][1]["oauthClientSecret"] == "secret-2-abc"


def test_a_push_the_workload_takes_on_a_later_attempt_is_a_rotation(db, r1, monkeypatch):
    """The first push fails, the second is taken: rotated, with one SME call and two pushes."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    _instance(db)
    r1.push = [503, 204]
    assert rotation.rotate_due(db) == {"rotated": 1} and r1.sme_posts() == 1 and len(r1.pushed) == 2


def test_a_rotation_writes_an_audit_row_and_an_event_and_neither_holds_a_secret(db, r1, monkeypatch):
    """One audit row (actor `system:secret-rotation`, role internal, on the instance's credentials) and one `credentials-rotated` event per rotation, the chain still
    verifies, and no secret, new or old, appears in either."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    iid = _instance(db)

    rotation.rotate_due(db)

    audit_rows, event_rows = _rows(db)
    assert len(audit_rows) == 1 and len(event_rows) == 1
    row = audit_rows[0]
    assert (row.actor, row.actor_role, row.action, row.target, row.result) == ("system:secret-rotation", "internal", "ROTATE_CREDENTIALS",
                                                                               f"/rapp-mgmt/instances/{iid}/credentials", "ok")
    assert row.detail == {"instanceId": str(iid), "oauthClientId": f"inv-{iid}", "olderThanDays": 30}
    event = events.view(event_rows[0])
    assert event["type"] == "org.oran.smo.rapp-instance.credentials-rotated" and event["source"] == "/smo/rapp-mgmt" and event["subject"] == str(iid)
    assert event["data"] == row.detail
    everything = json.dumps([audit.as_dict(a) for a in audit_rows] + [event])
    assert "secret-1-abc" not in everything and "oauthClientSecret" not in everything
    with db() as session:
        assert audit.verify(session) is None


def test_a_run_never_logs_the_secret(db, r1, monkeypatch, caplog):
    """Neither a rotation nor a failed push puts the new secret in the log."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    _instance(db)
    _instance(db)
    r1.push = [204, 503]
    with caplog.at_level("DEBUG"):
        rotation.rotate_due(db)
    assert "secret-" not in caplog.text and "oauthClientSecret" not in caplog.text and "secret rotation ran" in caplog.text


def test_an_instance_that_stopped_being_due_before_its_turn_is_skipped(db, r1, monkeypatch):
    """Renewed by hand, terminated or deleted between the list and the rotation: skipped, nothing called."""
    renewed = _instance(db, issued_days=1)
    gone = uuid.uuid4()
    terminated = _instance(db, state="UNDEPLOYED")
    counts = _outcomes()
    for iid in (renewed, gone, terminated):
        assert rotation.rotate_instance(db, iid, 30, _now()) == "skipped"
    assert r1.calls == [] and _rows(db) == ([], []) and _outcomes() == counts


def test_two_runners_do_not_rotate_the_same_instance_twice(db, r1, monkeypatch):
    """Two workers tick at once: the single runner gives the task to one of them, so SME is asked once and one rotation is audited; and a second run on its own finds
    nothing due."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    monkeypatch.setattr("smo_shared.db.SessionLocal", db)
    _instance(db)
    now = _now()

    first = tick(tasks.TASKS, module="rapp-mgmt", session_factory=db, now=now)
    second = tick(tasks.TASKS, module="rapp-mgmt", session_factory=db, now=now)

    assert first == {"rotate-credentials": "ran"} and second == {"rotate-credentials": "skipped"}
    assert r1.sme_posts() == 1 and len(_rows(db)[0]) == 1
    third = tick(tasks.TASKS, module="rapp-mgmt", session_factory=db, now=now + datetime.timedelta(seconds=tasks.TASKS[0].interval_seconds + 1))
    assert third == {"rotate-credentials": "ran"} and r1.sme_posts() == 1       # the next interval: nothing is due


def test_the_worker_task_does_nothing_while_the_rotation_is_off(db, r1, monkeypatch):
    """The task is always declared; with the setting at 0 its run changes nothing."""
    monkeypatch.setattr("smo_shared.db.SessionLocal", db)
    iid = _instance(db, issued_days=500)
    before = _issued_at(db, iid)
    tasks.rotate_credentials()
    assert r1.calls == [] and _issued_at(db, iid) == before


def test_the_worker_declares_one_task_with_a_configurable_interval(monkeypatch):
    """`rotate-credentials` every 900 s by default; the interval comes from `RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS`, never below a second, and a bad value gives the default."""
    assert [(t.name, t.interval_seconds, t.shared) for t in tasks.TASKS] == [("rotate-credentials", 900.0, False)]
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", "60")
    assert tasks._interval("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", 900.0) == 60.0
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", "0.1")
    assert tasks._interval("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", 900.0) == 1.0
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", "often")
    assert tasks._interval("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", 900.0) == 900.0


def test_issue_secret_returns_the_secret_or_none(r1):
    """SME's answer is the new secret; any refusal, or SME unreachable, is None (the caller decides what that means)."""
    inst = RAppInstance(instance_id=uuid.uuid4(), package_id=uuid.uuid4(), oauth_client_id="inv-1")
    assert issue_secret(inst) == "secret-1-abc"
    r1.sme = 503
    assert issue_secret(inst) is None
    r1.sme = httpx.ConnectError("down")
    assert issue_secret(inst) is None


def test_create_stamps_the_issue_time_and_the_detail_shows_it(client, db_session_factory, monkeypatch):
    """A new instance's secret is issued at create: the stamp is set and `GET /instances/{id}` shows it as `credentialsIssuedAt`; a row without one shows null."""
    created = _create(client, monkeypatch, operatorApiBase="http://energy-saving-rapp:8000")
    stamp = client.get(f"/instances/{created['instanceId']}").json()["credentialsIssuedAt"]
    assert _now() - datetime.datetime.fromisoformat(stamp) < datetime.timedelta(minutes=1)
    old = _instance(db_session_factory, issued_days=None)
    assert client.get(f"/instances/{old}").json()["credentialsIssuedAt"] is None


def test_a_renewal_by_hand_restarts_the_age_but_a_refused_one_does_not(client, db_session_factory, monkeypatch):
    """Re-send credentials moves the issue time to now (so the automatic rotation does not follow it at once); one the workload refuses (503) leaves it alone."""
    created = _create(client, monkeypatch, operatorApiBase="http://energy-saving-rapp:8000")
    iid = uuid.UUID(created["instanceId"])
    with db_session_factory() as session:
        session.get(RAppInstance, iid).credentials_issued_at = _now() - datetime.timedelta(days=90)
        session.commit()
    old = _issued_at(db_session_factory, iid)

    import app.main as main_module
    main_module.R1Client.post.push_status = 503
    assert client.post(f"/instances/{iid}/credentials/renew").status_code == 503
    assert _issued_at(db_session_factory, iid) == old
    main_module.R1Client.post.push_status = 204
    assert client.post(f"/instances/{iid}/credentials/renew").status_code == 200
    assert _now() - as_utc(_issued_at(db_session_factory, iid)) < datetime.timedelta(minutes=1)


def test_nothing_is_rotated_while_credentials_are_delivered_through_kubernetes_secrets(db, r1, monkeypatch, caplog):
    """With `RAPP_CREDENTIAL_DELIVERY=kubernetes` the workload reads its credentials from a Secret only the service rewrites: the worker rotates nothing and says so."""
    monkeypatch.setenv("RAPP_MGMT_SECRET_ROTATION_DAYS", "30")
    monkeypatch.setenv("RAPP_CREDENTIAL_DELIVERY", "kubernetes")
    iid = _instance(db)
    before = _issued_at(db, iid)
    assert rotation.rotate_due(db) == {}
    assert r1.calls == [] and _issued_at(db, iid) == before and "RAPP_CREDENTIAL_DELIVERY is kubernetes" in caplog.text
