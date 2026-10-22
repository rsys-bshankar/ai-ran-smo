"""Mock O1 Adaptor: an isolated NETCONF- and RESTCONF-shaped test double for RAN NF OAM's southbound O1 adaptor, with an emitting side and test-only controls.

What it is: one FastAPI app that answers, over plain HTTP, what RAN NF OAM sends a managed element's registered `adaptor_uri`: an RFC 6241 `<edit-config>` /
`<get-config>` RPC as XML (`POST /edit-config`, RAN NF OAM LLD section 5.1, client `ran-nf-oam/app/netconf_client.py`) and the RFC 8040 RESTCONF data resources
(`/restconf/data/...`, OI-1-cm-sync-restconf, client `ran-nf-oam/app/restconf_client.py`), both over one in-memory running configuration. It also declares itself
(`GET /capabilities`, read by RAN NF OAM's vendor onboarding), can emit alarms, PM, software-phase results and heartbeats towards RAN NF OAM on request (`/emit/*`; heartbeats also on its own, `keepalive.py`,
PR-SB-9b, SB-9.8; the target of the conformance kit in `conformance/o1`), and can become a vendor from a profile (`profile.py`, SB-10).
It stands in for the O-RAN-SC reference simulator (ntsim-ng), which is a YANG-validated NETCONF/SSH simulator out of proportion with this build's single-stack
design (HISTORY.md section 2: ADOPT repositories stay pattern references); it is deliberately minimal, "give the real caller something real to call", not a protocol
implementation.

What it owns: module-level dictionaries (`_applied_changes`, `_object_state`, `_faults`), so the state is per process, not thread-safe, and lost on restart or
`DELETE /state`. What it does not do: authenticate (nothing here is behind R1 Termination; it is a test double on the compose network), validate against YANG, or
speak SSH.

Before editing: the routes are mirrored by tests in `tests/` and by the conformance kit (`conformance/o1`, whose README lists each check), and the OpenAPI document
in `docs/openapi` is generated from the routes and models, so a change to a route or model needs the specs regenerated (`scripts/generate_openapi_specs.py`).
"""

import contextlib
import logging
import os
import uuid
from typing import Any
from urllib.parse import quote, unquote, urlencode
from xml.sax.saxutils import escape, quoteattr

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from smo_shared.webhook import is_safe_webhook_destination, post_webhook
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import install_health

from . import keepalive, profile as vendor_profile

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Starts the periodic heartbeat (`keepalive.py`, off unless MOCK_O1_HEARTBEAT_SECONDS is set) and stops it on shutdown."""
    stop = keepalive.start()
    yield
    if stop is not None:
        stop.set()


app = FastAPI(title="Mock O1 Adaptor (NETCONF and RESTCONF test double)", lifespan=_lifespan)
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_health(app)  # /live, /ready and /health for the compose healthcheck (PR-ST-7)

NETCONF_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"

DEFAULT_SERVICES = "PROV,FM,PM,FILE,STREAM,SWM,SUBSCRIPTION,HEARTBEAT"

# Module-level state, per process: written by the route handlers (a plain `def` route runs in a worker thread) without a lock, which is fine for a single-client test double.
_applied_changes: dict[str, dict] = {}  # managed-object ref -> last applied attribute_changes, for real test assertions

# Wave 10.1 (W10-17): the running configuration of each managed function
# addressed with `function-ref` (e.g. NRCellDU=101 inside gnb-du-01) —
# merged writes, read back by <get-config>. A function never written reads
# as its IOC's defaults: a cell is UNLOCKED and not energy saving.
_object_state: dict[tuple[str, str | None], dict] = {}
IOC_DEFAULTS = {
    "NRCellDU": {"administrativeState": "UNLOCKED", "operationalState": "ENABLED"},
    "CESManagementFunction": {"energySavingControl": "TO_BE_NOT_ENERGY_SAVING", "energySavingState": "IS_NOT_ENERGY_SAVING"},
    # Wave 10.2 (W10.2-04): a neighbour relation's CIO — TS 28.541's six
    # QOffsetRange entries (dB) — and whether handover over it is allowed
    "NRCellRelation": {"cellIndividualOffset": "[0, 0, 0, 0, 0, 0]", "isHOAllowed": "true", "isMLBAllowed": "true"},
    # the gNB's own distributed MRO (TS 28.541 DMROFunction) and its bounds
    "DMROFunction": {"dmroControl": "true", "maximumDeviationHoTriggerLow": "-12", "maximumDeviationHoTriggerHigh": "12",
                     "minimumTimeBetweenHoTriggerChange": "10", "tstoreUEcntxt": "100"},
    # Wave 10.3 (W10.3-04): a cell's coverage knobs — its beam's digital tilt
    # (TS 28.541, tenths of a degree, positive = downtilt) and its sector
    # carrier's maximum transmit power (dBm in this build)
    "CommonBeamformingFunction": {"digitalTilt": "60", "digitalAzimuth": "0", "coverageShape": "0"},
    "NRSectorCarrier": {"configuredMaxTxPower": "43", "txDirection": "DL_AND_UL"},
    # Wave 10.4 (W10.4-04): a cell's idle-mode relation to a frequency layer
    # (NRFreqRelation=<cell>-<layer>) — the reselection priority it broadcasts
    "NRFreqRelation": {"cellReselectionPriority": "5", "qOffsetFreq": "0"},
}
# TS 28.541 CESManagementFunction: energySavingState follows energySavingControl
ENERGY_SAVING_STATE = {"TO_BE_ENERGY_SAVING": "IS_ENERGY_SAVING", "TO_BE_NOT_ENERGY_SAVING": "IS_NOT_ENERGY_SAVING"}

# Test-only fault injection (see POST /faults): each entry is consumed by
# the next matching edit-config.
_faults: list[dict] = []


def _profile() -> dict | None:
    """SB-10: the vendor profile `MOCK_O1_PROFILE` names (app/profile.py), read on every request like the other settings; None without the variable. A name that
    cannot be loaded is an error at the request (and at start-up, below), not a silent fall back to the generic stub."""
    name = os.environ.get("MOCK_O1_PROFILE", "").strip()
    return vendor_profile.load(name) if name else None


_profile()      # a typo in MOCK_O1_PROFILE stops the container from starting (the call raises at import, so uvicorn never serves)


def _defaults(function_ref: str | None) -> dict:
    """The attribute defaults of the class named by `function_ref` (`<IOC>=<id>`): the built-in `IOC_DEFAULTS` for that class, overridden attribute by attribute by the active vendor profile's `iocDefaults`. Empty for an unknown class."""
    ioc = (function_ref or "").split("=", 1)[0]
    return {**IOC_DEFAULTS.get(ioc, {}), **((_profile() or {}).get("iocDefaults", {}).get(ioc, {}))}


def _vendor_modes() -> list[str]:
    """The O1 transports this adaptor declares: `MOCK_O1_VENDOR_MODES` (comma separated) when set, else the profile's, else both NETCONF and RESTCONF."""
    profile = _profile()
    return os.environ.get("MOCK_O1_VENDOR_MODES", "").split(",") if os.environ.get("MOCK_O1_VENDOR_MODES") else (
        list(profile["supportedVendorModes"]) if profile else ["O1_NETCONF", "O1_RESTCONF"])


def _restconf_unavailable() -> JSONResponse | None:
    """A vendor that declares no O1_RESTCONF has no RESTCONF root: its RESTCONF routes answer 404, as a real NETCONF-only adaptor's would (SB-10)."""
    if "O1_RESTCONF" in _vendor_modes():
        return None
    return _problem(404, "no RESTCONF", "this adaptor does not declare O1_RESTCONF in supportedVendorModes, so it has no RESTCONF root")


def _current(ref: str, function_ref: str | None) -> dict:
    """The running configuration of one managed function (or element, when `function_ref` is None): its class defaults with whatever has been written over them."""
    return {**_defaults(function_ref), **_object_state.get((ref, function_ref), {})}


def _take_fault(ref: str, function_ref: str | None) -> str | None:
    """Consumes one use of the first injected fault that matches `ref` and `function_ref` and returns its mode, or None when none matches.

    A fault with no `managedObjectRef` matches everything; otherwise it must equal `<ref>` or `<ref>/<function-ref>`. A fault is removed once its `count` reaches zero. Shared by the NETCONF and RESTCONF write paths.
    """
    for fault in _faults:
        target = fault.get("managedObjectRef")
        if target in (None, ref, f"{ref}/{function_ref}"):
            fault["count"] -= 1
            if fault["count"] <= 0:
                _faults.remove(fault)
            return fault["mode"]
    return None


SWM_NS = "urn:o-ran:software-management:1.0"
SWM_RPCS = ("software-download", "software-install", "software-activate")
_software_rpcs: list[dict] = []          # the software RPCs received, oldest first (GET /software-rpcs), for a test or a demo to look at


@app.get("/software-rpcs")
def software_rpcs() -> list[dict]:
    """The O-RAN software management RPCs this adaptor has acknowledged, oldest first: `{rpc, remote-file-path?, slot-name?, file-names?}` (call flow 19b)."""
    return list(_software_rpcs)


@app.post("/edit-config")
async def edit_config(request: Request) -> Response:
    """Mirrors netconf_client.py's own build_edit_config_rpc/send_edit_config
    exactly: parses the real <rpc><edit-config>...</edit-config></rpc>
    request, replies <rpc-reply><ok/></rpc-reply> (RFC 6241 section 4.2) on
    success. REJECTED (a real <rpc-error>, same as a real NETCONF agent
    refusing a request) when the managed-object ref is missing, or its
    config body is empty for any operation other than delete/remove — a
    delete legitimately carries no attribute_changes at all (RFC 6241
    section 7.2's `operation` attribute), so rejecting it for emptiness
    would be wrong. Non-delete/remove emptiness rejection is the same
    "empty payload is a real, testable rejection trigger" pattern.
    """
    # Maintainer note (not published). Always HTTP 200 with an `<rpc-reply>`, except an injected TIMEOUT fault, which is a 504 with a plain-text body. Order:
    # parse (defusedxml; malformed or entity-expanding XML is `malformed-message`) -> `<get-config>` is a read of the running configuration (`invalid-value` without
    # a ref) -> an edit with no ref, or with no attribute elements for an operation other than delete/remove, is `invalid-value` -> an injected fault is consumed
    # (TIMEOUT, RPC_ERROR) -> the change is applied, or only acknowledged under IGNORE_WRITE. `delete`/`remove` forget the object, which then reads as its class defaults.
    body = await request.body()
    try:
        # defusedxml (not stdlib ET) — this body is attacker-reachable over
        # HTTP, so entity expansion / external-entity XML needs to be
        # rejected the same way malformed XML is, not parsed.
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException):
        return _reply(message_id="0", ok=False, error_tag="malformed-message")

    message_id = root.attrib.get("message-id", "0")
    # Call flow 19b: an O-RAN software management RPC (software-download, -install, -activate) is acknowledged and recorded; the phase's completion is
    # reported separately (`POST /emit/software-phase`), as the element's download-event, install-event and activation-event would be.
    operation_rpc = next((child for child in root if child.tag in {f"{{{SWM_NS}}}{name}" for name in SWM_RPCS}), None)
    if operation_rpc is not None:
        name = operation_rpc.tag.rsplit("}", 1)[-1]
        _software_rpcs.append({"rpc": name, **{c.tag.rsplit("}", 1)[-1]: c.text for c in operation_rpc}})
        return _reply(message_id, ok=True)
    managed_object = root.find(f".//{{{NETCONF_BASE_NS}}}managed-object")
    ref = managed_object.attrib.get("ref") if managed_object is not None else None
    function_ref = managed_object.attrib.get("function-ref") if managed_object is not None else None
    if root.find(f"{{{NETCONF_BASE_NS}}}get-config") is not None:
        # Wave 10.1 (W10-20): read-after-write
        if not ref:
            return _reply(message_id, ok=False, error_tag="invalid-value")
        return _data_reply(message_id, ref, function_ref, _current(ref, function_ref))
    # RFC 6241 section 7.2's edit-config `operation` attribute — defaults
    # to "merge" per the RFC when absent, matching netconf_client.py's own
    # build_edit_config_rpc default.
    operation = managed_object.attrib.get("operation", "merge") if managed_object is not None else "merge"
    # child.tag carries the inherited default namespace (build_edit_config_rpc
    # declares xmlns once, on the <rpc> root — every descendant, including
    # each attribute-change element, inherits it) — strip it back to the
    # plain attribute name, the same rsplit("}", 1)[-1] send_edit_config's
    # own reply-parsing already uses for the same reason.
    attribute_changes = {
        child.tag.rsplit("}", 1)[-1]: child.text for child in managed_object
    } if managed_object is not None else {}

    if not ref or (not attribute_changes and operation not in ("delete", "remove")):
        return _reply(message_id, ok=False, error_tag="invalid-value")

    fault = _take_fault(ref, function_ref)
    if fault == "TIMEOUT":
        return Response(status_code=504, content="agent did not answer in time")
    if fault == "RPC_ERROR":
        return _reply(message_id, ok=False, error_tag="operation-failed")

    if operation in ("delete", "remove"):
        _applied_changes.pop(ref, None)
        _object_state.pop((ref, function_ref), None)
    elif fault != "IGNORE_WRITE":  # IGNORE_WRITE: acknowledged but never applied (a lying agent)
        _applied_changes[ref] = attribute_changes
        state = {**_object_state.get((ref, function_ref), {}), **attribute_changes}
        if "energySavingControl" in attribute_changes:
            state["energySavingState"] = ENERGY_SAVING_STATE.get(attribute_changes["energySavingControl"], "IS_NOT_ENERGY_SAVING")
        _object_state[(ref, function_ref)] = state
    return _reply(message_id, ok=True)


# ---------------------------------------------------------------- RESTCONF (OI-1-cm-sync-restconf)
#
# RFC 8040 over the same running configuration and fault injection as the
# NETCONF route above, mirroring ran-nf-oam/app/restconf_client.py: the
# RESTCONF root is /restconf, a managed object is the data resource
# /restconf/data/managed-element={ref}[/managed-function={function-ref}]
# with percent-encoded keys, bodies are application/yang-data+json
# (RFC 7951 list entries). Errors are `ietf-restconf:errors` bodies with
# RFC 8040 section 7's status codes. An object "exists" for create/delete
# once it has been written; a read of one never written answers its IOC
# defaults, as <get-config> does.

YANG_JSON = "application/yang-data+json"
RESTCONF_ERROR_STATUS = {"invalid-value": 400, "malformed-message": 400, "data-exists": 409, "data-missing": 404,
                         "operation-failed": 500, "operation-not-supported": 405}


@app.get("/.well-known/host-meta")
def restconf_root_discovery():
    """RFC 8040 section 3.1: where the RESTCONF root is."""
    # Maintainer note (not published). 404 (problem body) when the adaptor does not declare O1_RESTCONF, so a NETCONF-only vendor has no RESTCONF root at all.
    if (absent := _restconf_unavailable()) is not None:
        return absent
    return Response(content='<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">'
                            '<Link rel="restconf" href="/restconf"/></XRD>',
                    media_type="application/xrd+xml")


def _restconf_error(tag: str, message: str | None = None) -> JSONResponse:
    """An RFC 8040 section 7 error response (`ietf-restconf:errors` body, `application/yang-data+json`) for the error tag `tag`, with the status `RESTCONF_ERROR_STATUS` maps it to."""
    error = {"error-type": "application", "error-tag": tag}
    if message:
        error["error-message"] = message
    return JSONResponse({"ietf-restconf:errors": {"error": [error]}}, status_code=RESTCONF_ERROR_STATUS[tag],
                        media_type=YANG_JSON)


# The RESTCONF routes below share one rule for "where": `_restconf_target` reads the raw (still percent-encoded) path, so a `/` or `=` inside a key stays inside it.
def _restconf_target(request: Request) -> tuple[str | None, str | None] | None:
    """(ref, function-ref) addressed by the request's data-resource path, or
    None if it is not one this mock models. Parsed from the raw path, so a
    percent-encoded `/` or `=` inside a key stays inside it."""
    raw = request.scope.get("raw_path", b"").decode("latin-1").split("?", 1)[0]
    prefix = "/restconf/data"
    if not raw.startswith(prefix):
        return None
    ref = function_ref = None
    for segment in [p for p in raw[len(prefix):].split("/") if p]:
        name, _, key = segment.partition("=")
        if name == "managed-element" and ref is None and key:
            ref = unquote(key)
        elif name == "managed-function" and ref is not None and function_ref is None and key:
            function_ref = unquote(key)
        else:
            return None
    return ref, function_ref


async def _restconf_entry(request: Request, list_name: str, key: str) -> tuple[str | None, dict] | None:
    """The single list entry a request body carries: (its key, its other
    leaves), or None if the body is not that shape."""
    try:
        body = await request.json()
    except ValueError:
        return None
    entries = body.get(list_name) if isinstance(body, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        return None
    entry = dict(entries[0])
    return entry.pop(key, None), {k: str(v) for k, v in entry.items()}


def _restconf_fault(ref: str, function_ref: str | None):
    """Applies an injected fault to a RESTCONF write: a `Response` (504 for TIMEOUT, a 500 `operation-failed` error for RPC_ERROR) to return at once, else the fault mode (e.g. IGNORE_WRITE, for `_restconf_apply`) or None."""
    fault = _take_fault(ref, function_ref)
    if fault == "TIMEOUT":
        return Response(status_code=504, content="agent did not answer in time")
    if fault == "RPC_ERROR":
        return _restconf_error("operation-failed")
    return fault


def _restconf_apply(ref: str, function_ref: str | None, attributes: dict, replace: bool, fault) -> None:
    """Writes `attributes` to the running configuration of `(ref, function_ref)`: merged into what is there, or replacing it when `replace` is true.

    Always records the change in `_applied_changes`, and keeps `energySavingState` consistent with `energySavingControl`. Does nothing when `fault` is IGNORE_WRITE (the write is acknowledged by the caller but never applied: a lying agent).
    """
    if fault == "IGNORE_WRITE":  # acknowledged but never applied (a lying agent)
        return
    _applied_changes[ref] = attributes
    state = {} if replace else dict(_object_state.get((ref, function_ref), {}))
    state.update(attributes)
    if "energySavingControl" in attributes:
        state["energySavingState"] = ENERGY_SAVING_STATE.get(attributes["energySavingControl"], "IS_NOT_ENERGY_SAVING")
    _object_state[(ref, function_ref)] = state


# One registration per method, not api_route(methods=[...]): its methods are
# a set, so the generated operation ids (and the committed OpenAPI spec)
# would depend on the process's hash seed.
@app.get("/restconf/data/{path:path}")
@app.patch("/restconf/data/{path:path}")
@app.put("/restconf/data/{path:path}")
@app.delete("/restconf/data/{path:path}")
async def restconf_data(path: str, request: Request) -> Response:
    # Maintainer note (not published; this route has no docstring so the OpenAPI document stays without one). GET, PATCH, PUT and DELETE of one managed-element or
    # managed-function data resource (RFC 8040 sections 4.3 to 4.7). 404 when the adaptor declares no O1_RESTCONF; 400 `invalid-value` for a path this mock does not
    # model, a body key that differs from the path's key, or an empty PATCH; 400 `malformed-message` for a body that is not a single list entry; 404 `data-missing` for
    # a DELETE of an object never written; injected faults apply as for NETCONF. PATCH merges (and creates an object not yet written, which RFC 8040 would answer
    # 404 to), PUT replaces and answers 201 when it created the object, else 204. A GET of an unwritten object answers its class defaults.
    if (absent := _restconf_unavailable()) is not None:
        return absent
    ref, function_ref = _restconf_target(request) or (None, None)
    if ref is None:
        return _restconf_error("invalid-value", "not a managed-element / managed-function data resource")
    list_name, key = ("managed-function", "function-ref") if function_ref else ("managed-element", "ref")
    if request.method == "GET":
        entry = {key: function_ref or ref, **_current(ref, function_ref)}
        return JSONResponse({list_name: [entry]}, media_type=YANG_JSON)
    if request.method == "DELETE":
        fault = _restconf_fault(ref, function_ref)
        if isinstance(fault, Response):
            return fault
        if (ref, function_ref) not in _object_state:
            return _restconf_error("data-missing")
        _applied_changes.pop(ref, None)
        _object_state.pop((ref, function_ref), None)
        return Response(status_code=204)
    parsed = await _restconf_entry(request, list_name, key)
    if parsed is None:
        return _restconf_error("malformed-message")
    entry_key, attributes = parsed
    if entry_key != (function_ref or ref):
        return _restconf_error("invalid-value", "the body's key does not match the target resource")
    if request.method == "PATCH" and not attributes:
        return _restconf_error("invalid-value", "an empty merge changes nothing")
    fault = _restconf_fault(ref, function_ref)
    if isinstance(fault, Response):
        return fault
    existed = (ref, function_ref) in _object_state
    _restconf_apply(ref, function_ref, attributes, replace=request.method == "PUT", fault=fault)
    return Response(status_code=201 if request.method == "PUT" and not existed else 204)


@app.post("/restconf/data")
@app.post("/restconf/data/{path:path}")
async def restconf_create(request: Request, path: str = "") -> Response:
    """POST creates a child of the target (RFC 8040 section 4.4.1): a
    managed element under the datastore, or a managed function under its
    element. 409 data-exists if the child is already there."""
    # Maintainer note (not published). Besides the 409: 404 when the adaptor declares no O1_RESTCONF; 400 `invalid-value` for a parent that is not `/data` or a
    # managed-element; 400 `malformed-message` for a body that is not one list entry with its key; injected faults apply before the existence check, so a TIMEOUT
    # fault fires even for a child that exists.
    if (absent := _restconf_unavailable()) is not None:
        return absent
    parent = _restconf_target(request) if path else (None, None)
    if parent is None or parent[1] is not None:
        return _restconf_error("invalid-value", "a child can be created under /data or a managed-element only")
    ref = parent[0]
    list_name, key = ("managed-function", "function-ref") if ref else ("managed-element", "ref")
    parsed = await _restconf_entry(request, list_name, key)
    child_key, attributes = parsed or (None, {})
    if child_key is None:
        return _restconf_error("malformed-message")
    ref, function_ref = (ref, child_key) if ref else (child_key, None)
    fault = _restconf_fault(ref, function_ref)
    if isinstance(fault, Response):
        return fault
    if (ref, function_ref) in _object_state:
        return _restconf_error("data-exists")
    _restconf_apply(ref, function_ref, attributes, replace=True, fault=fault)
    return Response(status_code=201)


@app.get("/capabilities")
def declare_capabilities():
    """The vendor capability declaration RAN NF OAM's vendor onboarding
    flow discovers (Wave 9 W9-03, `POST /ran-nf-oam/vendor-onboarding`
    with `discoverFrom`, read at this fixed path on the adaptor's registered
    origin): which vendor this adaptor fronts, which MnS
    services it implements and which O1 transports it speaks. Configurable
    per deployment so one image can stand in for several vendors.
    """
    # Maintainer note (not published). Read on every request from the environment (`MOCK_O1_VENDOR_NAME`, `MOCK_O1_SUPPORTED_SERVICES`, `MOCK_O1_VENDOR_MODES`),
    # which win over the active profile; the same values gate the transports (`_restconf_unavailable`) and the `/emit/*` triggers (`_declared_services`).
    return {
        "vendorName": os.environ.get("MOCK_O1_VENDOR_NAME") or (_profile() or {}).get("vendorName", "mock-vendor"),
        "supportedServices": _declared_services(),
        "supportedVendorModes": _vendor_modes(),
    }


@app.get("/edit-config/{managed_object_ref}")
def query_last_applied(managed_object_ref: str):
    """Not part of the real NETCONF RPC surface — a test-only introspection
    route (matching this build's own `_policies`-dict-backed test doubles
    elsewhere) so a real integration test can assert what was actually
    applied, not just that the call returned 200.
    """
    # Maintainer note (not published). Always 200; `attributeChanges` is the attribute set of the last applied write for the ref, or null (never written, or deleted).
    # It does not see the per-function running configuration; `GET /objects/{ref}` does.
    return {"managedObjectRef": managed_object_ref, "attributeChanges": _applied_changes.get(managed_object_ref)}


# Request body of POST /faults (published in the OpenAPI document, so no docstring). `mode` is one of TIMEOUT, IGNORE_WRITE, RPC_ERROR (the route answers 422 for
# any other); `count` is how many matching writes misbehave before the fault is spent.
class FaultBody(BaseModel):
    mode: str  # TIMEOUT (HTTP 504) | RPC_ERROR (<rpc-error>) | IGNORE_WRITE (<ok/> but not applied)
    count: int = 1
    managedObjectRef: str | None = None  # "<ref>" or "<ref>/<function-ref>"; omitted = any


# The error body FastAPI answers for a body that is not JSON or the wrong shape (`{"detail": ...}`), declared so the OpenAPI document lists those responses.
class MockError(BaseModel):
    detail: str | list[dict]


@app.post("/faults", status_code=201, responses={400: {"model": MockError, "description": "the body is not JSON"}, 422: {"model": MockError, "description": "unknown fault mode, or a body of the wrong shape"}})
def inject_fault(body: FaultBody):
    """Test-only (like GET /edit-config/{ref}): make the next `count`
    matching edit-configs misbehave, so the SMO's retry, verification and
    rollback paths can be exercised against a real round trip."""
    # Maintainer note (not published). 201 with the pending faults; 422 for a mode other than TIMEOUT, RPC_ERROR or IGNORE_WRITE. Faults are consumed in the order
    # they were added by the first write that matches them (`_take_fault`); `DELETE /state` drops any that are left.
    if body.mode not in ("TIMEOUT", "RPC_ERROR", "IGNORE_WRITE"):
        return JSONResponse(status_code=422, content={"detail": f"unknown fault mode {body.mode}"})
    _faults.append(body.model_dump())
    return {"faults": _faults}


@app.delete("/state", status_code=204)
def reset_state():
    """Test-only: forget every applied change and pending fault."""
    # Maintainer note (not published). 204; clears the running configuration of every function as well, so everything reads as class defaults again.
    _applied_changes.clear()
    _object_state.clear()
    _faults.clear()
    _software_rpcs.clear()


@app.get("/objects/{managed_object_ref}")
def query_object(managed_object_ref: str, function_ref: str | None = None):
    """Test-only introspection of one managed function's running config."""
    # Maintainer note (not published). Always 200: the class defaults with the written attributes over them, the same view `<get-config>` and RESTCONF GET return.
    return {"managedObjectRef": managed_object_ref, "functionRef": function_ref,
            "attributes": _current(managed_object_ref, function_ref)}


# ---------------------------------------------------------------- the emitting side (PR-SB-9b, SB-9.8)
#
# Everything above answers RAN NF OAM (configuration, discovery). A real adaptor also *emits* towards RAN NF OAM: alarms, PM reports and files,
# software-management phase results, heartbeats. These routes make this stub do that on request, so it is a source of FM, PM, SWM and heartbeat
# for development and the target of the conformance kit's emit checks (conformance/o1: FM-*, PM-*, SW-*, HB-*). Each trigger POSTs the matching
# RAN NF OAM route (the query/body shapes of ran-nf-oam/app/main.py) and returns what came back. The target is MOCK_O1_OAM_URL, or the request's
# own `target`. Calls go over plain HTTP: with SMO_MTLS=on RAN NF OAM wants a client certificate this stub does not present (a known limit).

EMIT_TIMEOUT_SECONDS = 5.0
log = logging.getLogger(__name__)
# the trigger -> the MnS service that must be in /capabilities' supportedServices for the stub to emit it
EMIT_SERVICES = {"alarm": "FM", "pm-report": "PM", "pm-file": "FILE", "heartbeat": "HEARTBEAT", "software-phase": "SWM"}


# RFC 7807 problem body of the emit routes' error responses (409, 422, 502), declared in `EMIT_RESPONSES`; a model of the published OpenAPI document, so no docstring.
class Problem(BaseModel):
    type: str = "about:blank"
    title: str | None = None
    status: int | None = None
    detail: str | list[dict]


# Response of every `/emit/*` route: the outcome of the POST to RAN NF OAM, whatever RAN NF OAM answered (a published model, so no docstring).
class EmitResult(BaseModel):
    emitted: bool         # RAN NF OAM accepted it (a 2xx)
    status: int           # the HTTP status RAN NF OAM answered
    response: Any = None  # its body (JSON, else the first 500 characters of the text)
    target: str           # the RAN NF OAM origin it was sent to


# Base of the emit request bodies: the optional per-call RAN NF OAM origin. It is caller-supplied, so `_emit` checks it with `is_safe_webhook_destination` and sends through `post_webhook`.
class EmitTarget(BaseModel):
    target: str | None = None  # RAN NF OAM origin for this call, instead of MOCK_O1_OAM_URL (http/https, not a loopback or link-local literal)


# Request body of POST /emit/alarm; the fields become the query parameters of RAN NF OAM's `POST /alarms/ingest` (renamed to snake_case in `emit_alarm`).
class EmitAlarm(EmitTarget):
    managedElementRef: str
    severity: str  # a PerceivedSeverity: CRITICAL, MAJOR, MINOR, WARNING, INDETERMINATE or CLEARED, any case
    sourceAlarmId: str | None = None  # default: a new UUID, so every call raises a new alarm
    probableCause: str | None = None
    specificProblem: str | None = None
    managedFunctionRef: str | None = None
    alarmType: str | None = None
    correlationGroup: str | None = None


# One measurement of a PM report or file, in the shape RAN NF OAM's `POST /pm-reports` takes: `value` for a plain counter, or `values` (name -> number), plus the optional `relation`.
class EmitMeasurement(BaseModel):
    cellId: str
    timestamp: str  # ISO 8601
    value: float | None = None
    values: dict[str, float] | None = None
    relation: str | None = None


# Request body of POST /emit/pm-report; forwarded as the JSON body of `POST /pm-reports` without `target` and without unset fields.
class EmitPmReport(EmitTarget):
    managedElementRef: str
    counterType: str
    measurements: list[EmitMeasurement]


# Request body of POST /emit/pm-file: a PM report plus the file's metadata, forwarded as the JSON body of `POST /pm-files`.
class EmitPmFile(EmitPmReport):
    fileDataType: str = "Performance"
    fileFormat: str = "json"
    fileCompression: str | None = None
    jobId: str | None = None
    fileExpirationTime: str | None = None


# Request body of POST /emit/heartbeat: the id of the O1 adaptor endpoint registered at RAN NF OAM whose heartbeat is sent.
class EmitHeartbeat(EmitTarget):
    endpointId: str


# Request body of POST /emit/software-phase: the software-management job and whether its current phase succeeded (sent as the `succeeded` query parameter).
class EmitSoftwarePhase(EmitTarget):
    jobId: str
    succeeded: bool = True


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    """An RFC 7807 problem response (`application/problem+json`) with `status`, `title` and `detail`."""
    return JSONResponse(status_code=status, content={"type": "about:blank", "title": title, "status": status, "detail": detail},
                        media_type="application/problem+json")


def _declared_services() -> list[str]:
    """The MnS services this adaptor declares: `MOCK_O1_SUPPORTED_SERVICES` (comma separated, whitespace trimmed) when set, else the profile's, else `DEFAULT_SERVICES`. `/emit/*` refuses a service that is not in this list."""
    profile = _profile()
    fallback = ",".join(profile["supportedServices"]) if profile else DEFAULT_SERVICES
    return [s.strip() for s in os.environ.get("MOCK_O1_SUPPORTED_SERVICES", fallback).split(",") if s.strip()]


def _emit(kind: str, path: str, target: str | None, *, params: dict | None = None, body: dict | None = None) -> JSONResponse:
    """POSTs `path` (with `params` as the query string and `body` as JSON) to RAN NF OAM for the trigger `kind` and returns the outcome as a JSON response.

    Answers 409 when this adaptor does not declare the service for `kind` or no target is configured, 422 for an unusable `target`, 502 when RAN NF OAM cannot be
    reached; any answer from RAN NF OAM, a refusal included, is relayed with `emitted` saying whether it was a 2xx and the body (JSON, or the first 500 characters
    of text). The request's own `target` is caller-supplied, so it goes through the guarded sender `post_webhook` (no loopback, link-local or unspecified address); the
    operator-set `MOCK_O1_OAM_URL` is trusted and called with plain `httpx`. Side effect: one outbound HTTP POST.
    """
    service = EMIT_SERVICES[kind]
    if service not in _declared_services():
        return _problem(409, "service not declared", f"this adaptor does not declare {service} in supportedServices (MOCK_O1_SUPPORTED_SERVICES), so it does not emit {kind}")
    if target:
        if not is_safe_webhook_destination(target):
            return _problem(422, "unusable target", "target must be an http(s) origin that is not a loopback, link-local or unspecified address")
        origin = target
    else:
        origin = os.environ.get("MOCK_O1_OAM_URL", "")
    origin = origin.strip().rstrip("/")
    if not origin:
        return _problem(409, "no target configured", "no RAN NF OAM to emit to: set MOCK_O1_OAM_URL, or name a `target` in the request")
    if target:   # a caller-supplied origin goes through the guarded sender (smo_shared.webhook)
        url = origin + path + ("?" + urlencode(params) if params else "")
        resp = post_webhook(url, json=body or {}, timeout=EMIT_TIMEOUT_SECONDS)
        if resp is None:
            return _problem(502, "RAN NF OAM unreachable", f"POST to {kind} target failed")
    else:
        try:
            resp = httpx.post(origin + path, params=params, json=body, timeout=EMIT_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            log.warning("emit %s to %s%s failed: %s: %s", kind, origin, path, type(exc).__name__, exc)
            return _problem(502, "RAN NF OAM unreachable", f"POST {path} failed: {type(exc).__name__}")
    try:
        answer: Any = resp.json()
    except ValueError:
        answer = resp.text[:500]
    return JSONResponse(content={"emitted": 200 <= resp.status_code < 300, "status": resp.status_code, "response": answer, "target": origin})


_PROBLEM_JSON = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
EMIT_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": MockError, "description": "the body is not JSON"},
    409: {"model": Problem, "content": _PROBLEM_JSON, "description": "no RAN NF OAM target configured, or this adaptor does not declare the service"},
    422: {"model": Problem, "content": _PROBLEM_JSON, "description": "unusable target, or a body of the wrong shape"},
    502: {"model": Problem, "content": _PROBLEM_JSON, "description": "RAN NF OAM could not be reached"},
}


@app.post("/emit/alarm", response_model=EmitResult, responses=EMIT_RESPONSES)
def emit_alarm(body: EmitAlarm):
    """FM: raise an alarm at RAN NF OAM (`POST /alarms/ingest`; its fields are query parameters there). `sourceAlarmId` defaults to a new UUID."""
    # Maintainer note (not published). Fields left unset are not sent. Errors: see `_emit` (409, 422, 502); a body of the wrong shape is FastAPI's 422.
    params = {"source_alarm_id": body.sourceAlarmId or str(uuid.uuid4()), "managed_element_ref": body.managedElementRef, "severity": body.severity,
              "probable_cause": body.probableCause, "specific_problem": body.specificProblem, "managed_function_ref": body.managedFunctionRef,
              "alarm_type": body.alarmType, "correlation_group": body.correlationGroup}
    return _emit("alarm", "/alarms/ingest", body.target, params={k: v for k, v in params.items() if v is not None})


@app.post("/emit/pm-report", response_model=EmitResult, responses=EMIT_RESPONSES)
def emit_pm_report(body: EmitPmReport):
    """PM: report measurements (`POST /pm-reports`); RAN NF OAM needs a PM subscription on the element and counter."""
    return _emit("pm-report", "/pm-reports", body.target, body=body.model_dump(exclude={"target"}, exclude_none=True))


@app.post("/emit/pm-file", response_model=EmitResult, responses=EMIT_RESPONSES)
def emit_pm_file(body: EmitPmFile):
    """FILE: report a finished performance file (`POST /pm-files`); RAN NF OAM keeps it and lists it at `GET /files`."""
    return _emit("pm-file", "/pm-files", body.target, body=body.model_dump(exclude={"target"}, exclude_none=True))


@app.post("/emit/heartbeat", response_model=EmitResult, responses=EMIT_RESPONSES)
def emit_heartbeat(body: EmitHeartbeat):
    """HEARTBEAT: tell RAN NF OAM this adaptor's endpoint is alive (`POST /o1-adaptor-endpoints/{id}/heartbeat`)."""
    return _emit("heartbeat", f"/o1-adaptor-endpoints/{quote(body.endpointId, safe='')}/heartbeat", body.target)


@app.post("/emit/software-phase", response_model=EmitResult, responses=EMIT_RESPONSES)
def emit_software_phase(body: EmitSoftwarePhase):
    """SWM: report the result of the job's current phase (`POST /software-management-jobs/{id}/advance`); RAN NF OAM moves DOWNLOAD, INSTALL, ACTIVATE on."""
    return _emit("software-phase", f"/software-management-jobs/{quote(body.jobId, safe='')}/advance", body.target,
                 params={"succeeded": "true" if body.succeeded else "false"})


def _data_reply(message_id: str, ref: str, function_ref: str | None, attributes: dict) -> Response:
    """The `<rpc-reply>` of a `<get-config>`: the attributes of one managed object as XML, with every value taken from the request escaped."""
    # every value echoed back is escaped — it came from the request (attribute names are not: they are element names)
    function = f" function-ref={quoteattr(function_ref)}" if function_ref else ""
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in attributes.items())
    return Response(content=(f"<rpc-reply message-id={quoteattr(message_id)} xmlns=\"{NETCONF_BASE_NS}\"><data>"
                             f"<managed-object ref={quoteattr(ref)}{function}>{body}</managed-object></data></rpc-reply>"),
                    media_type="application/xml")


def _reply(message_id: str, *, ok: bool, error_tag: str | None = None) -> Response:
    """The `<rpc-reply>` of an `<edit-config>`: `<ok/>`, or an `<rpc-error>` with `error_tag` (RFC 6241 section 4.3), always HTTP 200 because an rpc-error is a protocol-level outcome."""
    if ok:
        body = f'<rpc-reply message-id="{message_id}" xmlns="{NETCONF_BASE_NS}"><ok/></rpc-reply>'
    else:
        body = (
            f'<rpc-reply message-id="{message_id}" xmlns="{NETCONF_BASE_NS}">'
            f"<rpc-error><error-type>application</error-type><error-tag>{error_tag}</error-tag>"
            f"<error-severity>error</error-severity></rpc-error></rpc-reply>"
        )
    return Response(content=body, media_type="application/xml")
