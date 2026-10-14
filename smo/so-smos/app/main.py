"""SO SMOS (Service Orchestration SMOS): the HTTP routes to submit, read, cancel and list multi-step service orders.

What it is: the FastAPI app of the `so-smos` module (R1 route `/so-smos` via R1 Termination). `POST /orders` runs a list of steps in order through the dispatch table in
`dispatch.py` and stores the executed steps on a `ServiceOrder` row. The other routes read, cancel and list those rows.

Where it sits: called by the GUI BFF and by SA SMOS (which reads an order to find a monitor's deployment); it calls RAN NF OAM, NFO, FOCOM and AIMgF through `R1Client`, one call
per step. Design: SMO Design v1.3 section 3.13 and SO/SA SMOS LLD section 1; README sections 1.5 and 2.

What it owns: the `service_order` table (`models.py`). It performs no step itself and does not roll back completed steps.

Before editing: execution is synchronous, inside the request, but the order row is committed before the first step and again before and after each step (see
`submit_service_order`), so a reader sees it run and a crash leaves the step that was in flight RUNNING. Every write of `steps` locks the row first (`_locked`), because a cancel
from another request may change the same list while the order runs.
"""

import uuid

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate

from .dispatch import Submitter, run_step
from .models import ServiceOrder

app = FastAPI(title="SO SMOS")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
apply_r1_gateway_security(app)
apply_correlation_id(app)


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


# Request body of POST /orders. `scope` is free text; `steps` is a list of dicts, each of which must carry a string `stepType` and `targetModule` (checked by the validator, a 422
# otherwise) plus whatever fields its dispatcher reads (README section 2.4). `rmihRegistration` is stored with the order and used by nothing else in this module.
# `requestedBy` is the submitter (stored as `submittedBy`, sent as RAN NF OAM's `requestedBy` on a CONFIG step) and `msacRole` the MSAC role sent with it; the GUI BFF sets both
# from the console user, as on RAN NF OAM's own config route. A step may not carry either (422): the identity and role of a write are the order's, not the step author's.
class SubmitOrderRequest(BaseModel):
    scope: str
    steps: list[dict]
    rmihRegistration: str = "so-smos"
    requestedBy: str = Field(default="so-smos", min_length=1, max_length=200)
    msacRole: str | None = None

    @field_validator("steps")
    @classmethod
    def _each_step_names_its_type_and_target(cls, steps: list[dict]) -> list[dict]:
        """Refuses (422) a step without a string `stepType` and `targetModule`, and a step that carries `requestedBy` or `msacRole`."""
        for step in steps:
            if not isinstance(step.get("stepType"), str) or not isinstance(step.get("targetModule"), str):
                raise ValueError("every step needs a string stepType and targetModule")
            if "requestedBy" in step or "msacRole" in step:
                raise ValueError("requestedBy and msacRole are fields of the order, not of a step")
        return steps


def order_status(steps: list[dict]) -> str:
    """The order's status from its steps: FAILED if a step failed, CANCELLED if a step was cancelled, COMPLETED if every step completed, IN_PROGRESS otherwise.

    An order left IN_PROGRESS with a RUNNING step and nothing running it was cut short by a crash: that step's outcome is unknown (call flow 10b).
    """
    statuses = [step["status"] for step in steps]
    if "FAILED" in statuses:
        return "FAILED"
    if "CANCELLED" in statuses:
        return "CANCELLED"
    if all(status == "COMPLETED" for status in statuses):
        return "COMPLETED"
    return "IN_PROGRESS"


def _view(order: ServiceOrder) -> dict:
    """The JSON of an order as every route returns it."""
    return {"orderId": str(order.order_id), "scope": order.scope, "status": order_status(order.steps), "submittedBy": order.submitted_by, "steps": order.steps,
            "homingDecision": order.homing_decision, "rmihRegistration": order.rmih_registration}


def _locked(db: Session, order_id: uuid.UUID) -> ServiceOrder:
    """The order re-read from the database under a row lock (`SELECT ... FOR UPDATE` on Postgres), so its `steps` are the latest and no other request changes them until commit."""
    return db.get(ServiceOrder, order_id, with_for_update=True, populate_existing=True)


def _set_step(db: Session, order_id: uuid.UUID, index: int, step: dict) -> None:
    """Replaces step `index` of the order with `step` and commits. A new list is assigned: an in-place change to a plain JSON column is not persisted."""
    order = _locked(db, order_id)
    order.steps = [step if i == index else s for i, s in enumerate(order.steps)]
    db.commit()


@app.post("/orders", status_code=202)
def submit_service_order(body: SubmitOrderRequest, db: Session = Depends(get_session)):
    # Maintainer notes (not published). Answers 202 with the order after its steps have run; a step failure is in the body (`status` FAILED, `error`), not an HTTP status.
    # The order is committed with every step PENDING before anything is dispatched. Each step is then committed RUNNING, dispatched, and committed with its outcome. Before a step
    # starts the row is re-read under a lock: a step a cancel has made CANCELLED is not started, and neither is anything after it. The first FAILED step halts the order (fail-fast).
    order = ServiceOrder(scope=body.scope, steps=[{**step, "status": "PENDING"} for step in body.steps], rmih_registration=body.rmihRegistration,
                         submitted_by=body.requestedBy)
    db.add(order)
    db.commit()
    order_id = order.order_id

    r1 = R1Client()
    submitter = Submitter(requested_by=body.requestedBy, msac_role=body.msacRole)
    for index, step in enumerate(body.steps):
        current = _locked(db, order_id).steps[index]
        if current["status"] != "PENDING":
            db.rollback()  # cancelled while an earlier step ran: release the lock and start nothing more
            break
        _set_step(db, order_id, index, {**current, "status": "RUNNING"})
        outcome = run_step(r1, step, submitter)
        _set_step(db, order_id, index, outcome)
        if outcome["status"] == "FAILED":
            break
    return _view(db.get(ServiceOrder, order_id, populate_existing=True))


def _order_or_404(db: Session, order_id: uuid.UUID) -> ServiceOrder:
    """Returns the order or raises the SERVICE_ORDER_NOT_FOUND framework error (404)."""
    order = db.get(ServiceOrder, order_id)
    if order is None:
        raise framework_error(FrameworkError.SERVICE_ORDER_NOT_FOUND, detail=f"unknown orderId {order_id}")
    return order


@app.get("/orders/{order_id}")
def query_order_status(order_id: uuid.UUID, db: Session = Depends(get_session)):
    # Maintainer notes (not published). 404 SERVICE_ORDER_NOT_FOUND for an unknown id. `homingDecision` is returned as stored; no route in this module writes it.
    return _view(_order_or_404(db, order_id))


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: uuid.UUID, db: Session = Depends(get_session)):
    """Makes every PENDING step of the order CANCELLED; a COMPLETED, FAILED or RUNNING step keeps its status.

    The order may still be running in another request: the row is read under a lock, and the running submit re-reads it before each step, so no step after a cancel starts. The step
    that is RUNNING finishes. `steps` is assigned a new list: an in-place change to a plain JSON column is not persisted.
    """
    # Maintainer notes (not published). 404 SERVICE_ORDER_NOT_FOUND for an unknown id. Cancelling twice, or an order with nothing PENDING, changes nothing and still answers 200.
    _order_or_404(db, order_id)
    order = _locked(db, order_id)
    order.steps = [{**step, "status": "CANCELLED"} if step["status"] == "PENDING" else step for step in order.steps]
    db.commit()
    return _view(order)


@app.get("/orders")
def list_service_orders(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """List read over ServiceOrder — only GET-by-id existed. Each order's
    own steps come back whole, same as query_order_status."""
    # Maintainer notes (not published). Paginated by smo_shared.pagination (ordered by primary key, order_id). Each item carries the whole `steps` list, so a page of large orders is large.
    page = paginate(db, select(ServiceOrder), limit, offset)
    return {**page, "items": [_view(o) for o in page["items"]]}
