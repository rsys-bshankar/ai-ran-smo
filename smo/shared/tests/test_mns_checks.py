"""The cross-cutting checks of the ProvMnS router (smo_shared/mns/checks.py, plan task S1.5): the caller read from the gateway's headers, placement, the
claim decision and its refusals, MSAC and its refusals, the audit row, the metric, and the steps through the router of the test-only module (scope and
MSAC filtering a read, a subtree delete refused whole, an Idempotency-Key replay, a traceparent kept, the audit row of each write).
"""

import json

import pytest
from mns_testmodule import BASE, Backend, RecordingHandler, _sqlite, build_tree, notifications
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import select

from smo_shared import audit, msac
from smo_shared import scope as claims
from smo_shared.idempotency import IdempotencyKey
from smo_shared.mns import MnsError, ProvMnS, provmns_router
from smo_shared.mns.checks import (INTERNAL, Caller, ClaimAuthorizer, MsacStep, PrefixPlacement, RouterChecks, ScopeStep, Target, no_placement,
                                   sub_networks_of, unmanaged)

DU = "SubNetwork=A/ManagedElement=1/GNBDUFunction=1"
DU_DN = DU.replace("/", ",")


def _error(call) -> tuple:
    with pytest.raises(MnsError) as caught:
        call()
    e = caught.value
    return (e.status, e.type, e.reason, e.bad_objects)


# ---------------------------------------------------------------------------- the caller

def test_caller_from_the_gateways_headers():
    """The invoker (the rApp an internal module acts for, else the caller's own id), the role, the console user (internal callers only), the claim."""
    rapp = Caller.from_headers({"X-R1-Role": "rapp", "X-R1-Invoker-Id": "r-1", "X-R1-On-Behalf-Of": "other", "X-R1-Acting-User": "eve",
                                "X-R1-Scope": '{"regions": ["n"]}'})
    assert rapp == Caller("r-1", "rapp", None, claims.Scope(regions=frozenset({"n"})))
    internal = Caller.from_headers({"X-R1-Role": "internal", "X-R1-Invoker-Id": "dme", "X-R1-On-Behalf-Of": "r-2", "X-R1-Acting-User": " alice "})
    assert (internal.invoker, internal.role, internal.acting_user, internal.scope) == ("r-2", "internal", "alice", None)
    assert Caller.from_headers({"X-R1-Role": "internal", "X-R1-Invoker-Id": "dme"}).invoker == "dme"
    assert Caller.from_headers({"X-R1-Role": "internal", "X-R1-Acting-User": "  "}).acting_user is None
    assert Caller.from_headers({}) == Caller()
    assert Caller.from_headers({"X-R1-Role": ""}).role is None
    assert (internal.actor, Caller(invoker="x").actor, Caller().actor) == ("alice", "x", "unauthenticated")
    assert INTERNAL == Caller(role="internal")


# ---------------------------------------------------------------------------- placement and the claim

def test_placement():
    """SubNetworks come from the DN; a prefix rule places a DN and everything below it, the longest prefix winning; no rule, no placement."""
    assert sub_networks_of("SubNetwork=A,SubNetwork=B,ManagedElement=1") == ("A", "B")
    assert sub_networks_of("ManagedElement=1") == ()
    assert no_placement("SubNetwork=A,ManagedElement=1") == Target("SubNetwork=A,ManagedElement=1", sub_networks=("A",))
    place = PrefixPlacement({"SubNetwork=A": {"region": "r", "tenant": "t", "siteCluster": "c"}, "SubNetwork=A,ManagedElement=2": {"region": "r2"}})
    assert place("SubNetwork=A,ManagedElement=1") == Target("SubNetwork=A,ManagedElement=1", "r", "t", "c", ("A",))
    assert place("SubNetwork=A,ManagedElement=2,X=1") == Target("SubNetwork=A,ManagedElement=2,X=1", "r2", None, None, ("A",))
    assert place("SubNetwork=AB") == Target("SubNetwork=AB", sub_networks=("AB",))
    assert place("SubNetwork=A") == Target("SubNetwork=A", "r", "t", "c", ("A",))


def test_claim_authorizer():
    """The ADR 0005 rule on the target's placement, whatever the operation."""
    target = Target("SubNetwork=A,ManagedElement=1", "r", "t", "c", ("A",))
    allow = ClaimAuthorizer()
    assert allow.permits(Caller(), "deleteMOI", target)
    assert allow.permits(Caller(scope=claims.Scope(regions=frozenset({"r"}), siteClusters=frozenset({"c"}), subNetworks=frozenset({"A"}))), "x", target)
    for scope in (claims.Scope(regions=frozenset({"q"})), claims.Scope(tenants=frozenset({"q"})), claims.Scope(siteClusters=frozenset({"q"})),
                  claims.Scope(subNetworks=frozenset({"B"}))):
        assert not allow.permits(Caller(scope=scope), "getMOIAttributes", target)


def test_scope_step_refusals():
    """A read outside the claim is a 404 as for a DN that does not exist; a write is 403 SCOPE_DENIED naming the DN."""
    step = ScopeStep()
    north = Caller(scope=claims.Scope(regions=frozenset({"north"})))
    assert step.allows(Caller(), "createMOI", "SubNetwork=A")
    assert _error(lambda: step.check(north, "getMOIAttributes", "SubNetwork=A")) == (404, "MOI_NOT_FOUND", "SubNetwork=A does not exist", ["SubNetwork=A"])
    for operation in ("createMOI", "modifyMOIAttributes", "deleteMOI"):
        assert _error(lambda op=operation: step.check(north, op, "SubNetwork=A")) == (
            403, "SCOPE_DENIED", "SubNetwork=A is outside the caller's scope", ["SubNetwork=A"])
    step.check(Caller(), "createMOI", "SubNetwork=A")


class OnlyReads:
    """An authorizer standing in for S1.14's: it sees the operation and allows reads only."""

    def permits(self, caller, operation, target):
        return operation == "getMOIAttributes"


def test_the_authorizer_is_pluggable():
    """The scope step asks whatever authorizer it is given, with the operation (how S1.14 replaces the claim with AccessRules)."""
    step = ScopeStep(authorizer=OnlyReads())
    step.check(Caller(), "getMOIAttributes", "SubNetwork=A")
    assert _error(lambda: step.check(Caller(), "createMOI", "SubNetwork=A"))[:2] == (403, "SCOPE_DENIED")


# ---------------------------------------------------------------------------- MSAC

def test_msac_step():
    """No provider: nobody is managed; rules decide on the DN path with the MSAC operation of the ProvMnS one; refusals 404 for a read, 403 for a write."""
    assert unmanaged(Caller()) is None and MsacStep().allows(Caller(), "deleteMOI", "SubNetwork=A")
    rules = [msac.Rule("/SubNetwork=A/*", frozenset({"read", "update"}), "ALLOW"), msac.Rule("/SubNetwork=A/ManagedElement=9", frozenset({"update"}), "DENY")]
    step = MsacStep(provider=lambda caller: rules if caller.invoker == "managed" else None)
    managed = Caller(invoker="managed")
    assert step.allows(managed, "getMOIAttributes", "SubNetwork=A,ManagedElement=1")
    assert step.allows(managed, "modifyMOIAttributes", "SubNetwork=A,ManagedElement=1")
    assert not step.allows(managed, "modifyMOIAttributes", "SubNetwork=A,ManagedElement=9")
    assert not step.allows(managed, "createMOI", "SubNetwork=A,ManagedElement=1")
    assert not step.allows(managed, "deleteMOI", "SubNetwork=A,ManagedElement=1")
    assert step.allows(Caller(invoker="free"), "deleteMOI", "SubNetwork=B")
    assert _error(lambda: step.check(managed, "getMOIAttributes", "SubNetwork=B")) == (404, "MOI_NOT_FOUND", "SubNetwork=B does not exist", ["SubNetwork=B"])
    assert _error(lambda: step.check(managed, "deleteMOI", "SubNetwork=A,ManagedElement=1")) == (
        403, "MSAC_ACCESS_DENIED", "no access rule allows delete on SubNetwork=A,ManagedElement=1", ["SubNetwork=A,ManagedElement=1"])
    step.check(managed, "getMOIAttributes", "SubNetwork=A,ManagedElement=1")


def test_router_checks_order_and_visibility():
    """Scope is checked before MSAC; `visible` needs both for a read."""
    north = Caller(invoker="m", scope=claims.Scope(regions=frozenset({"north"})))
    checks = RouterChecks(module="x", msac=MsacStep(provider=lambda caller: []))
    assert _error(lambda: checks.authorize(north, "createMOI", "SubNetwork=A"))[1] == "SCOPE_DENIED"
    assert _error(lambda: checks.authorize(Caller(invoker="m"), "createMOI", "SubNetwork=A"))[1] == "MSAC_ACCESS_DENIED"
    assert not checks.visible(Caller(invoker="m"), "SubNetwork=A") and not checks.visible(north, "SubNetwork=A")
    assert RouterChecks().visible(Caller(), "SubNetwork=A")
    assert (RouterChecks().module, RouterChecks().audit) == ("unknown", True)


# ---------------------------------------------------------------------------- through the router

def _app(backend: Backend, checks: RouterChecks) -> TestClient:
    """A client of a ProvMnS app with `checks`, on `backend`, serving four classes (GNBDUFunction with the recording handler)."""
    mns = ProvMnS(source="/smo/mns-check", system_dn="ManagementNode=mns-check", classes=("SubNetwork", "ManagedElement", "GNBDUFunction", "NRCellDU"),
                  checks=checks)
    mns.serve("GNBDUFunction", RecordingHandler())

    def get_session():
        with backend.session() as db:
            yield db

    app = FastAPI()
    app.include_router(provmns_router(mns, get_session))
    return TestClient(app)


def test_scoped_reads_are_filtered_and_deletes_refused_whole():
    """A SubNetwork claim on A reads A's subtree only where it is placed; a delete of a subtree with an instance the caller may not delete is refused,
    naming only the base; an unscoped caller is unaffected."""
    backend = _sqlite()
    rules = {"SubNetwork=A": {"region": "r"}, "SubNetwork=A,ManagedElement=2": {"region": "elsewhere"}}
    client = _app(backend, RouterChecks(module="mns-check", scope=ScopeStep(placement=PrefixPlacement(rules))))
    build_tree(client)
    scoped = {"x-r1-scope": '{"regions": ["r"]}'}
    flat = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "2"},
                      headers={**scoped, "accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert [r["objectInstance"] for r in flat.json()] == ["SubNetwork=A", "SubNetwork=A,ManagedElement=1"]
    every = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}'}, headers={**scoped, "accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert "SubNetwork=A,ManagedElement=2" not in [r["objectInstance"] for r in every.json()] and len(every.json()) == 6
    refused = client.delete(BASE + "SubNetwork=A", headers=scoped)
    assert (refused.status_code, refused.json()["type"], refused.json()["badObjects"]) == (403, "SCOPE_DENIED", ["SubNetwork=A"])
    assert refused.json()["reason"] == "SubNetwork=A contains instances outside the caller's reach"
    assert client.get(BASE + "SubNetwork=A/ManagedElement=2").status_code == 200
    assert client.delete(BASE + "SubNetwork=A").status_code == 204


def test_each_write_is_audited_in_its_transaction():
    """createMOI (201 and 200), modifyMOIAttributes and deleteMOI each add a chained row with the caller, method, target, result and detail; a refused
    write adds none; the audit step can be turned off."""
    backend = _sqlite()
    client = _app(backend, RouterChecks(module="mns-check"))
    who = {"x-r1-role": "rapp", "x-r1-invoker-id": "r-9"}
    client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers=who)
    client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "x"}}, headers=who)
    client.patch(BASE + "SubNetwork=A", json={"attributes": {"userLabel": "y"}}, headers={**who, "content-type": "application/merge-patch+json"})
    client.put(BASE + "SubNetwork=A/Nope=1", json={"id": "1"}, headers=who)
    client.delete(BASE + "SubNetwork=A", headers=who)
    with backend.session() as db:
        rows = [(r.actor, r.actor_role, r.action, r.target, r.result, r.detail) for r in db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq))]
        assert audit.verify(db) is None
    assert rows == [("r-9", "rapp", "PUT", "/ProvMnS/SubNetwork=A", "201", {"operation": "createMOI", "dn": "SubNetwork=A", "moiVersion": 1}),
                    ("r-9", "rapp", "PUT", "/ProvMnS/SubNetwork=A", "200", {"operation": "createMOI", "dn": "SubNetwork=A", "moiVersion": 2}),
                    ("r-9", "rapp", "PATCH", "/ProvMnS/SubNetwork=A", "200", {"operation": "modifyMOIAttributes", "dn": "SubNetwork=A", "moiVersion": 3}),
                    ("r-9", "rapp", "DELETE", "/ProvMnS/SubNetwork=A", "204", {"operation": "deleteMOI", "dn": "SubNetwork=A", "deleted": 1})]
    quiet = _sqlite()
    _app(quiet, RouterChecks(module="mns-check", audit=False)).put(BASE + "SubNetwork=A", json={"id": "A"})
    with quiet.session() as db:
        assert db.scalars(select(audit.AuditEntry)).first() is None


def test_idempotency_key_on_create(monkeypatch):
    """The repeat of a createMOI with the same key replays the first answer without a second notification; another body with that key, an invalid key,
    and a key still running are refused in the TS 28.532 shape."""
    backend = _sqlite()
    IdempotencyKey.__table__.create(backend.engine)
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    client = _app(backend, RouterChecks(module="mns-check"))
    key = {"idempotency-key": "k-1"}
    first = client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers=key)
    again = client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers=key)
    assert (first.status_code, again.status_code, again.headers["idempotent-replayed"], again.json()) == (201, 201, "true", first.json())
    assert len(notifications(backend)) == 1
    reused = client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "x"}}, headers=key)
    assert reused.json() == {"status": "422", "type": "IDEMPOTENCY_KEY_REUSED", "title": "IDEMPOTENCY_KEY_REUSED",
                             "reason": "this Idempotency-Key was used for a different request (method, path or payload)"}
    invalid = client.put(BASE + "SubNetwork=B", json={"id": "B"}, headers={"idempotency-key": ""})
    assert (invalid.status_code, invalid.json()["type"]) == (422, "IDEMPOTENCY_KEY_INVALID")
    refused = client.put(BASE + "SubNetwork=C", json={"id": "x"}, headers={"idempotency-key": "k-2"})
    retried = client.put(BASE + "SubNetwork=C", json={"id": "C"}, headers={"idempotency-key": "k-2"})
    assert (refused.status_code, retried.status_code) == (400, 201)        # a refused attempt released its key, so the repeat runs again
    assert client.put(BASE + "SubNetwork=D", json={"id": "D"}, headers={"idempotency-key": "k-3"}).status_code == 201


def test_problem_without_details():
    """A ProblemDetails refusal with no detail, or no ProblemDetails at all, still answers in the TS 28.532 shape."""
    from fastapi import HTTPException

    from smo_shared.mns.router import _refusal
    assert _refusal(HTTPException(409, detail={"title": "X"})).body("default") == {"status": "409", "type": "X", "reason": "", "title": "X"}
    assert _refusal(HTTPException(500, detail="text")).type == "INVALID_REQUEST"


def test_traceparent_is_kept(monkeypatch):
    """A request's traceparent is kept on the notification it causes, also without a tracing middleware."""
    backend = _sqlite()
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    client = _app(backend, RouterChecks(module="mns-check"))
    trace = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers={"traceparent": trace})
    client.put(BASE + "SubNetwork=B", json={"id": "B"})
    first, second = notifications(backend)
    assert first["traceparent"] == trace and "traceparent" not in second


def test_metric_labels():
    """One observation per operation, under the module, the served class or `other`, the operation and the outcome class."""
    backend = _sqlite()
    client = _app(backend, RouterChecks(module="metric-test"))

    def count(cls, operation, outcome):
        return REGISTRY.get_sample_value("smo_provmns_http_duration_seconds_count",
                                         {"module": "metric-test", "class": cls, "operation": operation, "outcome": outcome}) or 0
    labels = [("SubNetwork", "createMOI", "2xx"), ("SubNetwork", "getMOIAttributes", "2xx"), ("SubNetwork", "modifyMOIAttributes", "4xx"),
              ("SubNetwork", "deleteMOI", "2xx"), ("other", "createMOI", "4xx"), ("SubNetwork", "getMOIAttributes", "4xx")]
    before = [count(*label) for label in labels]       # the registry is the process's: a test run twice in one process starts above zero
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    client.get(BASE + "SubNetwork=A")
    client.patch(BASE + "SubNetwork=A", json={}, headers={"content-type": "text/plain"})
    client.delete(BASE + "SubNetwork=A")
    client.put(BASE + "Other=1", json={"id": "1"})
    client.get("/ProvMnS/19.2.0/SubNetwork=A")
    assert [count(*label) - first for label, first in zip(labels, before, strict=True)] == [1] * 6


# ---------------------------------------------------------------------------- what each step is handed (the mutation gate's exact cases)

class Recorder:
    """An authorizer and an MSAC provider that record what they are asked: (caller's invoker, operation, DN) and the callers."""

    def __init__(self):
        self.asked: list[tuple] = []
        self.callers: list = []

    def permits(self, caller, operation, target):
        self.asked.append((caller.invoker, operation, target.dn))
        return True

    def rules(self, caller):
        self.callers.append(caller)
        return None


def test_each_operation_asks_the_steps_with_its_own_name():
    """The authorizer is asked with the operation's ProvMnS name for the target, for every instance a scoped read returns and for every instance a
    delete removes; the MSAC provider is handed the caller itself."""
    backend = _sqlite()
    seen = Recorder()
    client = _app(backend, RouterChecks(module="mns-check", scope=ScopeStep(authorizer=seen), msac=MsacStep(provider=seen.rules)))
    who = {"x-r1-role": "rapp", "x-r1-invoker-id": "r-1"}
    client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers=who)
    client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1"}, headers=who)
    client.patch(BASE + "SubNetwork=A", json={"attributes": {"userLabel": "x"}}, headers={**who, "content-type": "application/merge-patch+json"})
    seen.asked.clear()
    scoped = {**who, "x-r1-scope": '{"regions": ["r"]}'}
    client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}'}, headers=scoped)
    assert seen.asked == [("r-1", "getMOIAttributes", "SubNetwork=A")] * 2 + [("r-1", "getMOIAttributes", "SubNetwork=A,ManagedElement=1")]
    seen.asked.clear()
    assert client.delete(BASE + "SubNetwork=A", headers=who).status_code == 204
    assert seen.asked == [("r-1", "deleteMOI", "SubNetwork=A"), ("r-1", "deleteMOI", "SubNetwork=A,ManagedElement=1"), ("r-1", "deleteMOI", "SubNetwork=A")]
    assert {caller.invoker for caller in seen.callers} == {"r-1"}


def test_writes_ask_with_their_names():
    """createMOI, modifyMOIAttributes and deleteMOI each ask the authorizer under their own name."""
    backend = _sqlite()
    seen = Recorder()
    client = _app(backend, RouterChecks(module="mns-check", scope=ScopeStep(authorizer=seen)))
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    client.patch(BASE + "SubNetwork=A", json={"attributes": {"userLabel": "x"}}, headers={"content-type": "application/merge-patch+json"})
    client.delete(BASE + "SubNetwork=A")
    assert [operation for _, operation, _ in seen.asked] == ["createMOI", "modifyMOIAttributes", "deleteMOI", "deleteMOI"]


def test_msac_rules_filter_an_unscoped_callers_read_and_refuse_a_delete():
    """A caller with no claim but with MSAC rules reads only what the rules allow (the read is restricted, not limited and returned as is), and may not
    delete a subtree holding an instance the rules do not let it delete."""
    backend = _sqlite()
    rules = [msac.Rule("/SubNetwork=A", frozenset({"read", "delete"}), "ALLOW"), msac.Rule("/SubNetwork=A/ManagedElement=1", frozenset({"read"}), "ALLOW")]
    provider = lambda caller: rules if caller is not None and caller.invoker == "m" else None   # noqa: E731
    client = _app(backend, RouterChecks(module="mns-check", msac=MsacStep(provider=provider)))
    build_tree(client)
    flat = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}'},
                      headers={"x-r1-invoker-id": "m", "accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert [r["objectInstance"] for r in flat.json()] == ["SubNetwork=A", "SubNetwork=A,ManagedElement=1"]
    refused = client.delete(BASE + "SubNetwork=A", headers={"x-r1-invoker-id": "m"})
    assert (refused.status_code, refused.json()["type"]) == (403, "SCOPE_DENIED")       # a subtree is refused as a whole, for either step


def test_the_default_checks_are_named_after_the_source():
    """Without checks of its own a producer's metric and audit name it by the last part of its source."""
    assert ProvMnS(source="/smo/a/echo-x", system_dn="ManagementNode=x").checks.module == "echo-x"


def test_the_metric_class_is_the_last_rdn_of_the_uri():
    """The class label is the class of the URI's last RDN; a malformed RDN is labelled by its class part; the duration is the operation's own."""
    backend = _sqlite()
    client = _app(backend, RouterChecks(module="metric-class"))

    def count(cls, outcome):
        return REGISTRY.get_sample_value("smo_provmns_http_duration_seconds_count",
                                         {"module": "metric-class", "class": cls, "operation": "createMOI", "outcome": outcome}) or 0

    def total():
        return sum(REGISTRY.get_sample_value("smo_provmns_http_duration_seconds_sum",
                                             {"module": "metric-class", "class": c, "operation": "createMOI", "outcome": o}) or 0
                   for c in ("SubNetwork", "ManagedElement") for o in ("2xx", "4xx"))
    before = (count("SubNetwork", "2xx"), count("ManagedElement", "2xx"), count("SubNetwork", "4xx"), total())
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1"})
    assert client.put(BASE + "SubNetwork=a=b", json={"id": "a"}).status_code == 400
    after = (count("SubNetwork", "2xx"), count("ManagedElement", "2xx"), count("SubNetwork", "4xx"), total())
    assert [a - b for a, b in zip(after[:3], before[:3], strict=True)] == [1, 1, 1]
    assert 0 <= after[3] - before[3] < 30


def test_the_caller_details_and_the_tenant_axis():
    """An internal caller without a console user has none; the claim authorizer reads the target's tenant."""
    assert Caller.from_headers({"X-R1-Role": "internal", "X-R1-Invoker-Id": "dme"}).acting_user is None
    target = Target("SubNetwork=A", "r", "t", None, ("A",))
    assert ClaimAuthorizer().permits(Caller(scope=claims.Scope(tenants=frozenset({"t"}))), "createMOI", target)


def test_the_audit_row_keeps_the_correlation_id():
    """The audit row of a write carries the correlation id of the request that made it."""
    from smo_shared.correlation import _current_correlation_id
    backend = _sqlite()
    token = _current_correlation_id.set("corr-7")
    try:
        with backend.session() as db:
            RouterChecks(module="x").record(db, Caller(invoker="i"), "deleteMOI", "SubNetwork=A", 204, {})
            db.commit()
    finally:
        _current_correlation_id.reset(token)
    with backend.session() as db:
        assert db.scalars(select(audit.AuditEntry.correlation_id)).one() == "corr-7"


def test_the_hierarchical_answer_pages_and_a_request_without_accept_gets_it():
    """The hierarchical answer carries the Link to the next page as the flat one does; a request with no Accept header at all gets the hierarchical
    answer as application/json."""
    backend = _sqlite()
    client = _app(backend, RouterChecks(module="mns-check"))
    build_tree(client)
    page = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "2"},
                      headers={"accept": "application/vnd.3gpp.object-tree-hierarchical+json"})
    assert page.headers["content-type"] == "application/vnd.3gpp.object-tree-hierarchical+json"
    assert page.headers["link"].endswith('; rel="next"') and "cursor=" in page.headers["link"]
    del client.headers["accept"]
    bare = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "2"})
    assert (bare.status_code, bare.headers["content-type"], "link" in bare.headers) == (200, "application/json", True)
    assert bare.json()["objectInstance"] == "SubNetwork=A"          # the hierarchical form, not the flat list
