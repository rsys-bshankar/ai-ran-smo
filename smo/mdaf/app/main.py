"""MDAF (Management Data Analytics Function): the app that stores analytics reports, tells subscribers about them and
publishes the DME-sourced rule (TS 28.104 MDA). The TS 28.104 resources (MDAFunction, MDARequest, MDAReport) are in `mda.py`,
mounted at the bottom of this file.

Where it sits: one FastAPI app behind R1 Termination at `/mdaf`. Producers (rApps through the SDK) publish reports; consumers
subscribe, query and create MDA requests. Outgoing calls: `GET /dme/data-jobs/{id}` for every report input source
(`_validate_input_sources_are_real_dme_artifacts`) and, from `mda.py`, AIMgF for drift forwarding. Notifications to subscribers
and request targets are written to the transactional outbox (`smo_shared.outbox`, PR-MSG-1.8) and sent after the commit.
`ran-analytics` is a separate producer registry; there is no call either way. Design records: `docs/ARCHITECTURE.md` (MDAF),
the MDAF section of `HISTORY.md` §7, and `docs/STANDARDS.md` decision D-9.

What it owns: reports, subscriptions (with their threshold bookkeeping) and, in `mda.py`, MDA functions, requests and
deliveries. What it does not own: producer registration (`ran-analytics`), and DME's O1 action path (MDAF only reads DME's
data path to prove a report's sources are real).

Before editing: `publish_report` and `mda.publish_mda_report` each end in one commit that persists the report, the outbox rows
and the threshold state together; notification helpers must not commit on their own. `mda.py` imports helpers from this file,
so it is imported last (see the end of the file). The docstrings of routes and request models are published in
`docs/openapi/mdaf.json`.
"""

import uuid

import httpx
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import acting_user, invoker_id
from smo_shared.r1_client import R1Client
from smo_shared.roles import ROLE_RAPP, role_of
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.outbox import enqueue
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .models import MDAFReport, MDASubscription

# The exact wire values of TS 28.104 ThresholdInfo.thresholdDirection; `subscribe_analytics` checks the body against it.
THRESHOLD_DIRECTIONS = {"UP", "DOWN", "UP_AND_DOWN"}  # TS28.104 ThresholdInfo.thresholdDirection's exact wire values


# One threshold of a subscription (TS 28.104 ThresholdInfo). `thresholdDirection` is a plain string here and is checked against
# `THRESHOLD_DIRECTIONS` by the route; the stricter `ts28104.ThresholdInfo` is the MDA request's.
class ThresholdInfo(BaseModel):
    monitoredMDAOutputIE: str
    thresholdDirection: str
    thresholdValue: float
    hysteresis: float = 0

app = FastAPI(title="MDAF")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)

_r1 = R1Client()


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


def _validate_input_sources_are_real_dme_artifacts(input_sources: list[uuid.UUID]) -> None:
    """Refuses a report whose inputs are not DME data jobs: one `GET /dme/data-jobs/{id}` over R1 per source.

    Raises 422 DME_ARTIFACT_NOT_FOUND for the first source DME does not answer 200 for. Only existence is proven, the data is
    not fetched. This is the data path of DME, never its O1 action path. A transport error talking to DME is not caught. An
    empty list passes.
    """
    for source_id in input_sources:
        resp = _r1.get(f"/dme/data-jobs/{source_id}")
        if resp.status_code != 200:
            raise framework_error(FrameworkError.DME_ARTIFACT_NOT_FOUND, detail=f"no such DME data job {source_id}")


def check_publisher(request: Request, analytics_type: str) -> str | None:
    """Who is publishing a report of `analytics_type` (call flow 08c), after checking that an rApp may: a caller with the rApp role must be a registered producer of
    that type at RAN Analytics (403 PRODUCER_NOT_REGISTERED; 502 UPSTREAM_FAILED when RAN Analytics cannot say). The console and SMO modules are not asked.

    Returns what the report records as `published_by`: the rApp's invoker id, else the console user (`smo-gui:<user>`) or the calling module's id.
    """
    caller = invoker_id(request)
    if role_of(request) == ROLE_RAPP:
        try:
            resp = _r1.get("/ran-analytics/producers", params={"producer_id": caller, "analytics_type": analytics_type})
        except httpx.HTTPError as exc:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"RAN Analytics did not answer ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"RAN Analytics answered {resp.status_code} to the producer check")
        if not resp.json().get("items"):
            raise framework_error(FrameworkError.PRODUCER_NOT_REGISTERED,
                                  detail=f"{caller} is not a registered producer of {analytics_type} (POST /ran-analytics/producers)")
        return caller
    return acting_user(request) or caller    # the console's header is already `smo-gui:<user>`


@app.post("/reports", status_code=201)
def publish_report(analytics_type: str, output: dict, input_sources: list[uuid.UUID], request: Request, scope: dict | None = None,
                   db: Session = Depends(get_session)):
    # Stores a report from a producer (201 `{reportId}`; 403 PRODUCER_NOT_REGISTERED for an rApp that is not a registered producer of the type; 422
    # DME_ARTIFACT_NOT_FOUND when an input source is unknown to DME; 502 when RAN Analytics cannot be asked).
    # Order: publisher checked, sources checked, the report added and flushed, subscribers notified (outbox rows, threshold state), then
    # `_deliver_legacy_report` matches the report against open MDA requests and commits everything in one transaction. Nothing is sent
    # if the commit fails.
    published_by = check_publisher(request, analytics_type)
    _validate_input_sources_are_real_dme_artifacts(input_sources)
    report = MDAFReport(analytics_type=analytics_type, output=output, input_sources=input_sources, scope=scope, published_by=published_by)
    db.add(report)
    db.flush()
    _notify_report_subscribers(db, report)
    # Wave 5: a producer-push report also satisfies matching open MDARequests. This commits: the report, its subscriber
    # notifications, the threshold state and the request deliveries are one transaction (PR-MSG-1.8).
    _deliver_legacy_report(db, report)
    return {"reportId": str(report.report_id)}


def _threshold_crossed(sub: MDASubscription, output: dict) -> bool:
    """Decides whether this report crosses any of the subscription's thresholds, and records which side of each it is on.

    Edge-triggered with a hysteresis band: a threshold fires when the value reaches it in the watched direction and the stored
    side for that metric is not already that side; a value beyond the band on the other side re-arms it; a value inside the band
    changes nothing. A metric missing from the report is skipped. The first report for a metric fires if it is already across the
    threshold (the stored side starts empty). Returns True if any threshold fired. Mutates `sub.threshold_state` (metric to
    "ABOVE" or "BELOW"); the caller commits it, whether or not anything fired. A subscription with no thresholds returns False.
    """
    if not sub.threshold_info:
        return False
    state = dict(sub.threshold_state or {})
    crossed = False
    for entry in sub.threshold_info:
        ie, direction = entry["monitoredMDAOutputIE"], entry["thresholdDirection"]
        if ie not in output:
            continue
        value, threshold, hysteresis = output[ie], entry["thresholdValue"], entry["hysteresis"]
        previous = state.get(ie)
        # Branches in order: a crossing in a watched direction (fires and records the side), else a value beyond the hysteresis
        # band (records the side without firing), else nothing.
        if direction in ("UP", "UP_AND_DOWN") and value >= threshold and previous != "ABOVE":
            crossed, state[ie] = True, "ABOVE"
        elif direction in ("DOWN", "UP_AND_DOWN") and value <= threshold and previous != "BELOW":
            crossed, state[ie] = True, "BELOW"
        elif value < threshold - hysteresis:
            state[ie] = "BELOW"
        elif value > threshold + hysteresis:
            state[ie] = "ABOVE"
        # inside the hysteresis band and no new crossing: leave state as-is
    sub.threshold_state = state
    return crossed


def _notify_report_subscribers(db: Session, report: MDAFReport) -> None:
    """Queues a notification for every subscription of the report's analytics type that should hear about it.

    A subscription without thresholds is notified for every report; one with thresholds only when `_threshold_crossed` says a
    threshold fired. The notification is an outbox row (`enqueue`), so it is sent after the caller's commit and not at all if the
    transaction rolls back. A subscription with no `notification_destination` is a polling consumer and gets no row (`enqueue`
    also drops a destination the SSRF guard refuses). Does not commit.
    """
    subs = db.scalars(select(MDASubscription).where(MDASubscription.analytics_type == report.analytics_type)).all()
    for sub in subs:
        crossed = _threshold_crossed(sub, report.output)
        if sub.threshold_info and not crossed:
            continue
        enqueue(db, sub.notification_destination, {
            "reportId": str(report.report_id), "analyticsType": report.analytics_type,
            "output": report.output, "inputSources": [str(s) for s in report.input_sources],
        })
    # No commit here (PR-MSG-1.8): the caller's commit (`_deliver`'s) persists the report, these outbox rows and
    # threshold_state, including for subscriptions that didn't cross (or have no destination), in one transaction.


# Body of `POST /subscriptions`; everything is optional, so an empty body subscribes to every report of the analytics type with no destination.
class SubscribeAnalyticsRequest(BaseModel):
    """`scope`'s wire shape changes here from a bare JSON body to this
    named model — FastAPI can't leave `scope` as the implicit unwrapped
    body once a second body-eligible field (`thresholdInfo`) exists
    alongside it. No real caller sets `scope` on a subscription today
    (grepped: ran-analytics never does, and no test did either before
    this pass), so this is a real but zero-blast-radius wire change.
    """

    scope: dict | None = None
    thresholdInfo: list[ThresholdInfo] | None = None
    # Wave 3 (cross-cutting standardization, Subscriptions): was a query
    # param — the one subscription-shaped resource in this build taking
    # its callback outside the body, unlike DME/Intent
    # Service's own notificationDestination body field it's now unified
    # with.
    notificationDestination: str | None = None


@app.post("/subscriptions", status_code=201)
def subscribe_analytics(analytics_type: str, requested_by: str, request: Request,
                         body: SubscribeAnalyticsRequest = SubscribeAnalyticsRequest(), db: Session = Depends(get_session)):
    """SubscribeAnalytics. A subscription belongs to who made it: for an rApp, `requested_by` is its own invoker id whatever it sent (call flow 08b)."""
    # Creates a subscription for `analytics_type` on behalf of `requested_by` (both query parameters). 201 `{subscriptionId}`; 422
    # SCHEMA_VALIDATION_FAILED for a threshold direction outside UP / DOWN / UP_AND_DOWN. The notification destination is not
    # checked here: an unsafe one is dropped at publish time (see `smo_shared.outbox.enqueue`), the subscription still exists.
    if body.thresholdInfo is not None:
        bad = [t.thresholdDirection for t in body.thresholdInfo if t.thresholdDirection not in THRESHOLD_DIRECTIONS]
        if bad:
            raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"unknown thresholdDirection {bad[0]!r}")
    if role_of(request) == ROLE_RAPP:
        requested_by = invoker_id(request) or requested_by
    sub = MDASubscription(analytics_type=analytics_type, requested_by=requested_by, notification_destination=body.notificationDestination,
                          scope=body.scope, threshold_info=[t.model_dump() for t in body.thresholdInfo] if body.thresholdInfo else None)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id)}


@app.delete("/subscriptions/{subscription_id}", status_code=204)
def unsubscribe_analytics(subscription_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """UnsubscribeAnalytics. An rApp removes only its own subscriptions (403 SUBSCRIPTION_ACCESS_DENIED), so it cannot silence another consumer, an SA SMOS
    monitor's among them (call flow 04a); the console and SMO modules may remove any."""
    # Deletes a subscription; an unknown id is 204 as well (idempotent).
    sub = db.get(MDASubscription, subscription_id)
    if sub is None:
        return
    if role_of(request) == ROLE_RAPP and sub.requested_by != invoker_id(request):
        raise framework_error(FrameworkError.SUBSCRIPTION_ACCESS_DENIED, detail=f"subscription {subscription_id} belongs to {sub.requested_by}")
    db.delete(sub)
    db.commit()


@app.get("/subscriptions")
def list_analytics_subscriptions(analytics_type: str | None = None, requested_by: str | None = None,
                                  limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """HISTORY.md §5: no list/query endpoint for active
    subscriptions existed at all — the reference defines this route
    (even though its own implementation of it is a no-op stub; ours
    actually reads real, persisted subscriptions).
    """
    stmt = select(MDASubscription)
    if analytics_type:
        stmt = stmt.where(MDASubscription.analytics_type == analytics_type)
    if requested_by:
        stmt = stmt.where(MDASubscription.requested_by == requested_by)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_subscription_view(s) for s in page["items"]]}


@app.get("/reports")
def query_analytics_report(analytics_type: str | None = None, limit: int = PageLimit, offset: int = PageOffset,
                            db: Session = Depends(get_session)):
    # Paginated reports, optionally of one analytics type, ordered by report id (the paging default), which is not time order.
    # The view carries the report kind and MDA type.
    stmt = select(MDAFReport)
    if analytics_type:
        stmt = stmt.where(MDAFReport.analytics_type == analytics_type)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"reportId": str(r.report_id), "analyticsType": r.analytics_type, "output": r.output,
                               "reportKind": r.report_kind, "mdaType": r.mda_type, "publishedBy": r.published_by} for r in page["items"]]}


@app.get("/reports/{report_id}")
def get_analytics_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    """One report, as the list shows it plus its input sources. A subscriber that acts on a notification reads the report back here rather than trusting the
    pushed copy (SA SMOS, call flow 04b). 404 MDAF_REPORT_NOT_FOUND."""
    r = db.get(MDAFReport, report_id)
    if r is None:
        raise framework_error(FrameworkError.MDAF_REPORT_NOT_FOUND, detail=f"no report {report_id}")
    return {"reportId": str(r.report_id), "analyticsType": r.analytics_type, "output": r.output, "reportKind": r.report_kind, "mdaType": r.mda_type,
            "inputSources": [str(s) for s in r.input_sources], "publishedBy": r.published_by}


def _subscription_view(s: MDASubscription) -> dict:
    """The JSON of a subscription, including its thresholds (not the internal threshold state)."""
    return {"subscriptionId": str(s.subscription_id), "analyticsType": s.analytics_type,
            "requestedBy": s.requested_by, "notificationDestination": s.notification_destination, "scope": s.scope,
            "thresholdInfo": s.threshold_info}


# ---------------------------------------------------------------- Wave 5: TS 28.104 MDA NRM resources
# Imported last: app/mda.py reuses the helpers above. Bound at load time,
# never imported lazily inside a route (the integration mesh's loader
# evicts `app.*` from sys.modules after loading each service).
# Imported last on purpose: `mda.py` imports helpers from this module, so a top-of-file import would be circular.
from .mda import deliver_legacy_report as _deliver_legacy_report, router as _mda_router  # noqa: E402

app.include_router(_mda_router)
