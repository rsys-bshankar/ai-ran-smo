"""Tenant and region authorization (smo_shared/scope.py, docs/adr/0005-tenant-region-authorization.md): the claim, the semantic table, the header, what an SMO
module passes on when it acts for an rApp, and the claim of the person the operator's console acts for (GUI-5).

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_scope.py -q
"""

import itertools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from smo_shared import r1_client, scope
from smo_shared.correlation import apply_correlation_id
from smo_shared.invoker import ACTING_USER_HEADER, INVOKER_ID_HEADER, ON_BEHALF_OF_HEADER
from smo_shared.roles import ROLE_HEADER
from smo_shared.scope import ACTING_USER_SCOPE_HEADER, DENY_ALL, ON_BEHALF_SCOPE_HEADER, SCOPE_HEADER, Scope


def S(regions=None, tenants=None):
    """Helper: a Scope from lists of regions and tenants (None means that axis is not restricted)."""
    return Scope(regions=None if regions is None else frozenset(regions), tenants=None if tenants is None else frozenset(tenants))


# ---- the claim

def test_a_claim_is_parsed_per_axis_and_an_empty_one_is_no_claim():
    """A claim restricts only the axes it names, and an empty or missing claim means no claim."""
    assert scope.from_claim({"regions": ["eu-west"]}) == S(regions=["eu-west"])
    assert scope.from_claim({"tenants": ["acme", "globex"]}) == S(tenants=["acme", "globex"])
    assert scope.from_claim({"regions": ["a"], "tenants": ["b"]}) == S(["a"], ["b"])
    assert scope.from_claim({"regions": None, "tenants": ["b"]}) == S(tenants=["b"])
    assert scope.from_claim({}) is None and scope.from_claim(None) is None


# Table: claims that must be refused (wrong types, empty or duplicate values, bad characters, over-long values or lists, unknown axes); the error text
# never repeats the input.
@pytest.mark.parametrize("claim", [
    [], "eu", 3, {"regions": []}, {"regions": "eu"}, {"regions": ["eu", "eu"]}, {"regions": [""]}, {"regions": [" eu"]}, {"regions": ["-eu"]},
    {"regions": ["a" * 101]}, {"regions": [1]}, {"regions": ["eu west"]}, {"regions": ["eu\n"]}, {"regions": [f"r{i}" for i in range(101)]},
    {"region": ["eu"]}, {"regions": ["eu"], "cells": ["c"]}, {"tenants": [None]}, {"tenants": []},
])
def test_a_claim_that_is_not_valid_is_refused_with_a_fixed_message(claim):
    with pytest.raises(ValueError) as caught:
        scope.from_claim(claim)
    assert "eu west" not in str(caught.value) and "cells" not in str(caught.value)       # never the input back


def test_the_largest_valid_claim_and_the_longest_value_are_accepted():
    """A claim of 100 values and a 100 character value are accepted, so the limits are inclusive."""
    assert scope.from_claim({"regions": [f"r{i}" for i in range(100)]}) is not None
    assert scope.from_claim({"tenants": ["a" * 100]}) == S(tenants=["a" * 100])
    assert scope.valid_value("eu-west-1") and scope.valid_value("tenant:acme/1@x+y") and not scope.valid_value(None) and not scope.valid_value("")


def test_the_claim_round_trips_sorted_and_only_with_the_axes_that_restrict():
    """A scope encodes to a compact sorted claim naming only the axes that restrict, and decodes back to the same scope."""
    assert scope.to_claim(S(["b", "a"], None)) == {"regions": ["a", "b"]}
    assert scope.to_claim(S(None, ["t"])) == {"tenants": ["t"]}
    assert scope.to_claim(None) is None and scope.to_claim(Scope()) is None
    assert scope.encode(S(["b", "a"], ["t"])) == '{"regions":["a","b"],"tenants":["t"]}'
    assert scope.encode(None) is None and scope.encode(Scope()) is None
    assert scope.decode(scope.encode(S(["b", "a"], ["t"]))) == S(["a", "b"], ["t"])


def test_a_header_that_cannot_be_read_permits_nothing_and_no_header_is_no_claim():
    """A scope header that is damaged or invalid denies everything, while no header (or `{}`) is no claim; even absurd nesting is a refusal, not an
    error.
    """
    assert scope.decode(None) is None and scope.decode("") is None
    for damaged in ("not json", "[]", '{"regions":[]}', '{"regions":"x"}', '{"tenants":["a b"]}', '{"cells":["c"]}', '{"regions":["' + "a" * 200 + '"]}'):
        assert scope.decode(damaged) == DENY_ALL, damaged
    assert scope.decode("{}") is None
    assert not scope.permits(DENY_ALL, "eu", "acme") and not scope.permits(DENY_ALL, None, None)
    assert scope.decode("[" * 100000) == DENY_ALL                                          # nesting too deep for the parser is still a refusal, not an error


# ---- the semantic table (docs/adr/0005 section "Semantics")

def test_the_semantic_table():
    """Checks scope.permits against an independently written rule for every combination of caller scope, target region and target tenant."""
    regions, tenants = [None, "eu", "us"], [None, "acme", "globex"]
    callers = {"unscoped": None, "eu": S(["eu"]), "acme": S(tenants=["acme"]), "eu+acme": S(["eu"], ["acme"]), "eu,us": S(["eu", "us"]),
               "eu+acme,globex": S(["eu"], ["acme", "globex"])}
    for name, caller in callers.items():
        for region, tenant in itertools.product(regions, tenants):
            want = True
            if caller is not None:
                if caller.regions is not None:
                    want = want and region in caller.regions
                if caller.tenants is not None:
                    want = want and tenant in caller.tenants
            assert scope.permits(caller, region, tenant) is want, (name, region, tenant)


def test_the_rows_that_matter():
    """The cases of the ADR table that are easy to get wrong: unscoped callers see everything, a target with no region is hidden from a
    region-restricted caller, both axes must match, matching is exact.
    """
    assert scope.permits(None, None, None)                                 # unscoped sees an unscoped target
    assert scope.permits(None, "eu", "acme")                               # ... and a scoped one: nothing changes on upgrade
    assert not scope.permits(S(["eu"]), None, "acme")                      # a target with no region is not for a caller that restricts regions
    assert scope.permits(S(["eu"]), "eu", None)                            # ... but a tenant the claim does not restrict may be unset
    assert not scope.permits(S(["eu"], ["acme"]), "eu", None)              # both axes must match
    assert not scope.permits(S(["eu"]), "EU", None)                        # exact, case-sensitive
    assert not scope.permits(S(["eu"]), "eu-west", None)                   # no prefix or wildcard matching
    assert scope.permits(Scope(), None, None)                              # a claim that restricts nothing is no restriction


# ---- the query form of the same rule

class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "row"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    region: Mapped[str | None] = mapped_column(String)
    tenant: Mapped[str | None] = mapped_column(String)


def test_the_query_form_gives_the_same_answer_as_permits_for_every_row():
    """The SQL filter and the denied condition select exactly the rows that permits allows and refuses, for every kind of caller."""
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    rows = [(f"{r}-{t}", r, t) for r, t in itertools.product([None, "eu", "us"], [None, "acme", "globex"])]
    with Session(engine) as db:
        db.add_all(_Row(name=n, region=r, tenant=t) for n, r, t in rows)
        db.commit()
        for caller in (None, S(["eu"]), S(tenants=["acme"]), S(["eu"], ["acme"]), S(["eu", "us"]), DENY_ALL, S(None, [])):
            found = set(db.scalars(scope.filter_statement(select(_Row.name), caller, _Row.region, _Row.tenant)))
            assert found == {n for n, r, t in rows if scope.permits(caller, r, t)}, caller
            refused = set(db.scalars(select(_Row.name).where(scope.denied_condition(caller, _Row.region, _Row.tenant))))
            assert refused == {n for n, r, t in rows if not scope.permits(caller, r, t)}, caller        # the same rule, as the rows it refuses
            assert found | refused == {n for n, r, t in rows} and not found & refused


def test_a_caller_covers_what_it_hands_out_and_never_more():
    """A caller can hand out only a scope no wider than its own on every axis; no claim is the widest and DENY_ALL covers only itself."""
    assert scope.covers(None, None) and scope.covers(None, S(["eu"]))                      # an unscoped caller may hand out anything
    assert scope.covers(S(["eu", "us"]), S(["eu"])) and scope.covers(S(["eu"]), S(["eu"]))
    assert not scope.covers(S(["eu"]), None)                                                # no claim is the widest
    assert not scope.covers(S(["eu"]), S(["us"])) and not scope.covers(S(["eu"]), S(["eu", "us"]))
    assert not scope.covers(S(["eu"]), S(tenants=["acme"]))                                 # it does not restrict regions, so it is wider on that axis
    assert scope.covers(S(["eu"]), S(["eu"], ["acme"]))                                     # narrower on another axis as well is fine
    assert scope.covers(S(tenants=["acme"]), S(["eu"], ["acme"])) and not scope.covers(S(["eu"], ["acme"]), S(["eu"]))
    assert not scope.covers(DENY_ALL, S(["eu"])) and scope.covers(DENY_ALL, DENY_ALL)


def test_a_claim_from_introspection_that_is_damaged_permits_nothing():
    """A malformed claim in a token introspection answer becomes DENY_ALL rather than being ignored."""
    assert scope.from_introspection(None) is None and scope.from_introspection({}) is None
    assert scope.from_introspection({"regions": ["eu"]}) == S(["eu"])
    assert scope.from_introspection({"regions": []}) == DENY_ALL and scope.from_introspection("x") == DENY_ALL


# ---- which claim applies to a request

RAPP = {ROLE_HEADER: "rapp", INVOKER_ID_HEADER: "es-client", SCOPE_HEADER: '{"regions":["eu"]}'}
MODULE = {ROLE_HEADER: "internal", INVOKER_ID_HEADER: "dme-client"}
MODULE_FOR_RAPP = {**MODULE, ON_BEHALF_OF_HEADER: "es-client", ON_BEHALF_SCOPE_HEADER: '{"tenants":["acme"]}'}


def test_the_claim_that_applies_is_the_callers_own_or_the_one_a_module_passed_on():
    """The claim in force is the rApp's own, or the one an internal module passed on for it; an rApp's own on-behalf header and an unstamped request
    are not believed.
    """
    assert scope.scope_of(RAPP) == S(["eu"])
    assert scope.scope_of(MODULE_FOR_RAPP) == S(tenants=["acme"])               # DME's call for an rApp is limited as the rApp is
    assert scope.scope_of(MODULE) is None                                       # a module on its own account is unscoped
    assert scope.scope_of({}) is None
    assert scope.scope_of({**MODULE, ON_BEHALF_OF_HEADER: "es-client"}) is None  # acting for an unscoped rApp: no claim passed on
    assert scope.scope_of({**RAPP, ON_BEHALF_SCOPE_HEADER: '{"regions":["us"]}'}) == S(["eu"])    # an rApp's own on-behalf claim is not believed
    assert scope.scope_of({**RAPP, ON_BEHALF_OF_HEADER: "other", ON_BEHALF_SCOPE_HEADER: "{}"}) == S(["eu"])
    assert scope.scope_of({**MODULE_FOR_RAPP, SCOPE_HEADER: '{"regions":["us"]}'}) == S(tenants=["acme"])  # the module's own claim is not the rApp's
    assert scope.scope_of({**MODULE_FOR_RAPP, ON_BEHALF_SCOPE_HEADER: "garbage"}) == DENY_ALL
    assert scope.scope_of({ON_BEHALF_OF_HEADER: "x", ON_BEHALF_SCOPE_HEADER: '{"regions":["eu"]}'}) is None   # no role stamp: not through R1, not believed


CONSOLE = {ROLE_HEADER: "internal", INVOKER_ID_HEADER: "gui-client", ACTING_USER_HEADER: "smo-gui:ana"}


def test_the_console_passes_the_persons_claim_and_it_only_narrows():
    """GUI-5: the console's call for a person is limited by that person's claim; a person without one is unscoped; the console's own claim still bounds it; a damaged
    claim permits nothing; the person's claim is believed only from an internal caller that names the person, and the rApp passed-on claim still comes first."""
    assert scope.scope_of({**CONSOLE, ACTING_USER_SCOPE_HEADER: '{"regions":["eu"]}'}) == S(["eu"])
    assert scope.scope_of(CONSOLE) is None                                                         # a person with no claim: unscoped, as before
    bounded = {**CONSOLE, SCOPE_HEADER: '{"regions":["eu","us"],"tenants":["acme"]}', ACTING_USER_SCOPE_HEADER: '{"regions":["us","ap"]}'}
    assert scope.scope_of(bounded) == S(["us"], ["acme"])                                          # both: the person can never widen the console's claim
    assert scope.scope_of({**CONSOLE, SCOPE_HEADER: '{"tenants":["acme"]}'}) == S(tenants=["acme"])  # the console's own claim holds for an unscoped person
    assert scope.scope_of({**CONSOLE, ACTING_USER_SCOPE_HEADER: "garbage"}) == DENY_ALL
    assert scope.scope_of({**CONSOLE, ACTING_USER_HEADER: "", ACTING_USER_SCOPE_HEADER: '{"regions":["eu"]}'}) is None   # no person named: not believed
    assert scope.scope_of({**RAPP, ACTING_USER_HEADER: "x", ACTING_USER_SCOPE_HEADER: '{"regions":["us"]}'}) == S(["eu"])  # an rApp's is not believed
    assert scope.scope_of({ACTING_USER_HEADER: "x", ACTING_USER_SCOPE_HEADER: '{"regions":["eu"]}'}) is None             # no role stamp: not through R1
    assert scope.scope_of({**MODULE_FOR_RAPP, ACTING_USER_HEADER: "x", ACTING_USER_SCOPE_HEADER: '{"regions":["eu"]}'}) == S(tenants=["acme"])


def test_two_claims_narrow_axis_by_axis():
    """`narrowed` keeps, per axis, the values both list, the one axis that restricts, or nothing; no shared value permits nothing on that axis."""
    assert scope.narrowed(None, None) is None
    assert scope.narrowed(S(["eu"]), None) == S(["eu"]) == scope.narrowed(None, S(["eu"]))
    assert scope.narrowed(S(["eu", "us"]), S(["us"], ["acme"])) == S(["us"], ["acme"])
    assert scope.narrowed(S(tenants=["a", "b"]), S(["eu"], ["b", "c"])) == S(["eu"], ["b"])
    nothing = scope.narrowed(S(["eu"]), S(["us"]))
    assert nothing == S([]) and not scope.permits(nothing, "eu", None) and not scope.permits(nothing, "us", None)


def test_the_claim_of_a_request_is_read_from_its_headers():
    """request_scope reads the claim in force from a request's headers."""
    from types import SimpleNamespace
    assert scope.request_scope(SimpleNamespace(headers=RAPP)) == S(["eu"])
    assert scope.request_scope(SimpleNamespace(headers=MODULE_FOR_RAPP)) == S(tenants=["acme"])
    assert scope.request_scope(SimpleNamespace(headers=MODULE)) is None


@pytest.fixture
def service():
    """A test service with correlation and invoker context installed and a route that records the originator's scope and the headers R1Client would
    send onward; returns the client and the record.
    """
    app = FastAPI()
    apply_correlation_id(app)
    sent = {}

    @app.get("/inside")
    def inside():
        # Test route recording the originator's scope and the onward headers; not part of any published API.
        sent["scope"] = scope.get_originator_scope()
        sent["headers"] = r1_client.R1Client(bearer_token="t")._headers()
        return {}

    return TestClient(app), sent


def test_r1client_passes_the_rapps_claim_on_beside_its_id(service):
    """R1Client sends the rApp's scope claim beside its id on onward calls."""
    client, sent = service
    client.get("/inside", headers=RAPP)
    assert sent["scope"] == S(["eu"])
    assert sent["headers"][ON_BEHALF_OF_HEADER] == "es-client" and sent["headers"][ON_BEHALF_SCOPE_HEADER] == '{"regions":["eu"]}'


def test_the_claim_holds_across_modules_and_is_not_invented(service):
    """The claim is passed on across modules and is not invented when a module acts for nobody or for an unscoped rApp."""
    client, sent = service
    client.get("/inside", headers=MODULE_FOR_RAPP)
    assert sent["headers"][ON_BEHALF_SCOPE_HEADER] == '{"tenants":["acme"]}'
    client.get("/inside", headers=MODULE)                                        # a module acting for nobody says nothing
    assert ON_BEHALF_SCOPE_HEADER not in sent["headers"] and sent["scope"] is None
    client.get("/inside", headers={ROLE_HEADER: "rapp", INVOKER_ID_HEADER: "es-client"})   # an unscoped rApp: its id, no claim
    assert sent["headers"][ON_BEHALF_OF_HEADER] == "es-client" and ON_BEHALF_SCOPE_HEADER not in sent["headers"]
    assert ON_BEHALF_SCOPE_HEADER not in r1_client.R1Client(bearer_token="t")._headers()      # outside any request


def test_a_claim_that_was_damaged_stays_a_refusal_when_passed_on(service):
    """A damaged claim stays DENY_ALL when R1Client passes it on, so it cannot become unrestricted at the next hop."""
    client, sent = service
    client.get("/inside", headers={**RAPP, SCOPE_HEADER: "garbage"})
    assert sent["scope"] == DENY_ALL
    assert scope.decode(sent["headers"][ON_BEHALF_SCOPE_HEADER]) == DENY_ALL
