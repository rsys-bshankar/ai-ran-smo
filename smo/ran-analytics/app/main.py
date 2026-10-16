"""RAN Analytics: the registry of use-case analytics producers (traffic, energy, coverage and similar), each declaring the DME input
types it reads and the output schema it publishes.

Where it sits: one FastAPI app behind R1 Termination at `/ran-analytics`. Producer rApps register, list and deregister through the
SDK (`sdk.analytics`) and the GUI BFF registers. A registration also enrols the producer in SME (provider registration, then a
published service API `mdaf.<analyticsType>.<producerId>`), the only outgoing calls; MDAF asks the list route whether a publishing
rApp is a registered producer of the report's type (call flow 08c). Design records: `HISTORY.md` §7 (the `analytics_type` and
MDAType finding), `HISTORY.md` DOC-CF-1.8 flow 08 and `docs/ARCHITECTURE.md` (MDAF).

Who may register: an rApp registers (and deregisters) only itself as the producer (its invoker id); the console and SMO modules may
name any producer.

What it owns: the `mdaf_producer` table, keyed by (producer id, analytics type). It does not own reports, subscriptions or the
TS 28.104 MDA resources.

Before editing: the docstrings of the routes are published in `docs/openapi/ran-analytics.json`. SME is asked before the producer
row is committed, so a producer that is stored is discoverable.
"""

import uuid
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import invoker_id
from smo_shared.r1_client import R1Client
from smo_shared.roles import ROLE_RAPP, role_of
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .models import MDA_TYPES, MDAFProducer, infer_mda_type

app = FastAPI(title="RAN Analytics SMOS")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


@app.post("/producers", status_code=201)
def register_analytics_producer(producer_id: str, analytics_type: str, dme_input_types: list[uuid.UUID], output_schema: dict, request: Request,
                                 mda_type: str | None = None, db: Session = Depends(get_session)):
    """RegisterAnalyticsProducer — an update-in-place upsert on
    (producer_id, analytics_type), not just an insert: the same producer
    re-registering the same analytics_type (e.g. on restart) is a normal
    occurrence, not a conflict. An rApp registers only itself (its own
    invoker id) as the producer.

    The producer is enrolled in SME and its service API
    `mdaf.<analyticsType>.<producerId>` published before it is stored, so
    every stored producer is discoverable; SME refusing or not answering
    stores nothing.

    HISTORY.md §7's `analytics_type` enum finding: `mda_type`, TS28104's
    own real closed MDAType enum, is optional and additive — a caller may
    declare one directly (validated against the real 24 values), or, if
    omitted, `infer_mda_type` derives it for the two shorthand values
    this build already honestly maps; anything else stays `None`, not
    guessed.
    """
    # Registers or re-registers a producer for an analytics type. 201 `{status: "registered", smeServiceId}`; 403 PRODUCER_IDENTITY_MISMATCH for an rApp naming
    # another producer; 422 SCHEMA_VALIDATION_FAILED for an `mda_type` outside the TS 28.104 list; 502 UPSTREAM_FAILED when SME refuses or does not answer (nothing
    # stored). The query carries `producer_id`, `analytics_type` and `mda_type`; `dme_input_types` and `output_schema` are body fields. The DME input type ids are
    # stored as given, not looked up. `mda_type` is the caller's, or inferred from the analytics type for the two known shorthands, else None.
    _refuse_another_rapp(request, producer_id, "register")
    if mda_type is not None and mda_type not in MDA_TYPES:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"unknown mda_type {mda_type!r}")
    service_id = _publish_in_sme(producer_id, analytics_type)
    prod = db.get(MDAFProducer, (producer_id, analytics_type))
    if prod is None:
        prod = MDAFProducer(producer_id=producer_id, analytics_type=analytics_type)
        db.add(prod)
    prod.dme_input_types = dme_input_types
    prod.output_schema = output_schema
    prod.mda_type = mda_type if mda_type is not None else infer_mda_type(analytics_type)
    prod.sme_service_id = service_id
    db.commit()
    return {"status": "registered", "smeServiceId": service_id}


def _refuse_another_rapp(request: Request, producer_id: str, action: str) -> None:
    """403 PRODUCER_IDENTITY_MISMATCH when the caller is an rApp and `producer_id` is not its own invoker id; the console and SMO modules may name any producer."""
    if role_of(request) == ROLE_RAPP and invoker_id(request) != producer_id:
        raise framework_error(FrameworkError.PRODUCER_IDENTITY_MISMATCH,
                              detail=f"an rApp may {action} only itself as a producer ({invoker_id(request)}), not {producer_id}")


def _publish_in_sme(producer_id: str, analytics_type: str) -> str:
    """Enrols the producer in SME (provider registration, an upsert) and publishes `mdaf.<analyticsType>.<producerId>`; returns SME's service id.
    502 UPSTREAM_FAILED when SME refuses either call or does not answer."""
    # HISTORY.md §5: SME's register_service requires the apf_id to be a registered publishing function, the same two-step CAPIF enrolment the reference requires.
    # One service per producer and type: SME names are unique across producers, so a shared `mdaf.<type>` made a second producer of a type undiscoverable (409).
    try:
        enrolled = R1Client().post("/sme/provider-registrations", json={"apfId": producer_id})
        if enrolled.status_code >= 300:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME refused to enrol {producer_id} ({enrolled.status_code})")
        # The producer id is URL-quoted with no safe characters, so an id with a slash or other reserved character stays one path segment.
        published = R1Client().post(f"/sme/published-apis/v1/{quote(producer_id, safe='')}/service-apis", json={
            "serviceName": f"mdaf.{analytics_type}.{producer_id}", "producerId": producer_id, "endpoint": "internal",
            "version": "1.0", "serviceCapabilities": {"analyticsType": analytics_type}, "moduleScope": "ran-analytics",
        })
    except httpx.HTTPError as exc:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME did not answer ({type(exc).__name__}); nothing stored") from exc
    if published.status_code >= 300:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME refused the service mdaf.{analytics_type}.{producer_id} ({published.status_code})")
    return str(published.json()["serviceId"])


@app.delete("/producers/{producer_id}", status_code=204)
def deregister_analytics_producer(producer_id: str, analytics_type: str, request: Request, db: Session = Depends(get_session)):
    """DeregisterAnalyticsProducer: withdraws the producer's SME service API and removes its registration for `analytics_type`. An rApp deregisters only itself.
    Reports it published stay at MDAF; once deregistered, an rApp may not publish more of that type."""
    # 204; 403 PRODUCER_IDENTITY_MISMATCH; 404 PRODUCER_NOT_FOUND; 502 UPSTREAM_FAILED when SME does not answer (the registration is kept). SME answers 204 for a
    # service it no longer has, so a withdrawal already done does not block the removal.
    _refuse_another_rapp(request, producer_id, "deregister")
    prod = db.get(MDAFProducer, (producer_id, analytics_type))
    if prod is None:
        raise framework_error(FrameworkError.PRODUCER_NOT_FOUND, detail=f"{producer_id} is not a registered producer of {analytics_type}")
    if prod.sme_service_id:
        try:
            withdrawn = R1Client().delete(f"/sme/published-apis/v1/{quote(producer_id, safe='')}/service-apis/{prod.sme_service_id}")
        except httpx.HTTPError as exc:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME did not answer ({type(exc).__name__}); the producer is kept") from exc
        if withdrawn.status_code >= 300 and withdrawn.status_code != 404:
            raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"SME refused to withdraw the service ({withdrawn.status_code}); the producer is kept")
    db.delete(prod)
    db.commit()


@app.get("/producers")
def list_analytics_producers(analytics_type: str | None = None, producer_id: str | None = None, limit: int = PageLimit,
                              offset: int = PageOffset, db: Session = Depends(get_session)):
    """HISTORY.md §5: no list/query endpoint for registered
    producers existed at all — same gap as MDAF's own subscriptions list.
    """
    # Paginated producers, filtered by analytics type and producer id (both optional, ANDed), in primary-key order.
    stmt = select(MDAFProducer)
    if analytics_type:
        stmt = stmt.where(MDAFProducer.analytics_type == analytics_type)
    if producer_id:
        stmt = stmt.where(MDAFProducer.producer_id == producer_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_producer_view(p) for p in page["items"]]}


def _producer_view(p: MDAFProducer) -> dict:
    """The JSON of a producer: ids, MDA type (or None), DME input types as text, the output schema as stored and its SME service id (None for one registered before revision 0056)."""
    return {"producerId": p.producer_id, "analyticsType": p.analytics_type, "mdaType": p.mda_type,
            "dmeInputTypes": [str(t) for t in p.dme_input_types], "outputSchema": p.output_schema, "smeServiceId": p.sme_service_id}
