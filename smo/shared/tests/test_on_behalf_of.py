"""Who a request is for (smo_shared/invoker.py): an rApp's id travels through the SMO modules acting for it, so the per-rApp safeguards at RAN NF OAM
apply to an rApp that writes through DME, and an rApp cannot pose as another.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_on_behalf_of.py -q
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from smo_shared import invoker, r1_client
from smo_shared.correlation import apply_correlation_id
from smo_shared.invoker import ACTING_USER_HEADER, INVOKER_ID_HEADER, ON_BEHALF_OF_HEADER, acting_user, get_originator, invoker_id, originator_of
from smo_shared.roles import ROLE_HEADER

RAPP = {ROLE_HEADER: "rapp", INVOKER_ID_HEADER: "es-client"}
MODULE = {ROLE_HEADER: "internal", INVOKER_ID_HEADER: "dme-client"}
MODULE_FOR_RAPP = {**MODULE, ON_BEHALF_OF_HEADER: "es-client"}


def test_the_originator_is_the_rapp_that_called_or_whom_a_module_passed_on():
    """The originator is the calling rApp's own id, or what an internal module passed on, and None for a module acting on its own account or a call
    that did not come through R1.
    """
    assert originator_of(RAPP) == "es-client"
    assert originator_of(MODULE_FOR_RAPP) == "es-client"                  # a module acting for an rApp
    assert originator_of(MODULE) is None                                    # a module acting on its own account
    assert originator_of({}) is None                                        # not through R1 at all


def test_an_rapp_cannot_name_somebody_else_as_the_originator():
    """An on-behalf-of header sent by an rApp is ignored, so one rApp cannot act as another."""
    assert originator_of({**RAPP, ON_BEHALF_OF_HEADER: "other-client"}) == "es-client"


def _request(headers):
    """Helper: sends a request with `headers` to a route that records invoker_id(request), and returns what it saw."""
    app = FastAPI()
    seen = {}

    @app.get("/who")
    def who(request: Request):
        # Test route recording the effective invoker; not part of any published API.
        seen["invoker"] = invoker_id(request)
        return {}

    TestClient(app).get("/who", headers=headers)
    return seen["invoker"]


def test_the_invoker_the_safeguards_see_is_the_rapp_behind_a_module():
    """The invoker the per-rApp safeguards apply to is the rApp behind an internal module's call, and the module's own id when it acts for nobody."""
    assert _request(RAPP) == "es-client"
    assert _request(MODULE_FOR_RAPP) == "es-client"                        # DME's call to RAN NF OAM is the rApp's
    assert _request(MODULE) == "dme-client"                                 # a module on its own account is itself
    assert _request({}) is None


def test_the_header_is_only_believed_from_an_internal_caller():
    """The on-behalf-of header counts only from a caller R1 stamped as internal; without a role stamp it is not trusted."""
    assert _request({**RAPP, ON_BEHALF_OF_HEADER: "other-client"}) == "es-client"
    assert _request({INVOKER_ID_HEADER: "x", ON_BEHALF_OF_HEADER: "other-client"}) == "x"        # no role stamp: not through R1, not trusted


@pytest.fixture
def service():
    """A test service built like every module (correlation and invoker context installed) with a route that records the originator and the headers
    R1Client would send onward; returns the client and the record.
    """
    app = FastAPI()
    apply_correlation_id(app)
    sent = {}

    @app.get("/inside")
    def inside():
        # Test route recording the originator and the headers R1Client would send onward; not part of any published API.
        sent["originator"] = get_originator()
        sent["headers"] = r1_client.R1Client(bearer_token="t")._headers()
        return {}

    return TestClient(app), sent


def test_r1client_tells_the_next_module_who_the_request_is_for(service):
    """While handling an rApp's request, R1Client adds that rApp as X-R1-On-Behalf-Of on onward calls."""
    client, sent = service
    client.get("/inside", headers=RAPP)
    assert sent["originator"] == "es-client" and sent["headers"][ON_BEHALF_OF_HEADER] == "es-client"


def test_the_chain_holds_across_modules_and_is_not_invented(service):
    """The originator is passed on through a chain of modules, but a module acting for nobody (or a call outside R1) adds no header."""
    client, sent = service
    client.get("/inside", headers=MODULE_FOR_RAPP)
    assert sent["headers"][ON_BEHALF_OF_HEADER] == "es-client"
    client.get("/inside", headers=MODULE)                                   # a module acting for nobody says nothing
    assert ON_BEHALF_OF_HEADER not in sent["headers"]
    client.get("/inside")
    assert ON_BEHALF_OF_HEADER not in sent["headers"]


def test_outside_a_request_nothing_is_added():
    """Outside any request there is no originator and R1Client adds no on-behalf-of header."""
    assert get_originator() is None
    assert ON_BEHALF_OF_HEADER not in r1_client.R1Client(bearer_token="t")._headers()


def test_one_request_does_not_leak_its_originator_into_the_next(service):
    """The originator is reset after each request, so a later request never inherits an earlier rApp's id."""
    client, sent = service
    client.get("/inside", headers=RAPP)
    client.get("/inside", headers=MODULE)
    assert sent["originator"] is None and invoker.get_originator() is None


def test_a_module_can_act_on_its_own_account_for_a_while_inside_a_request():
    """What the platform does about an rApp (not for it): R1Client adds neither the id nor the claim, and the request goes on as the rApp's afterwards."""
    app = FastAPI()
    apply_correlation_id(app)
    seen = {}

    @app.get("/inside")
    def inside():
        # Test route reading the originator and onward headers before, during and after an on_own_account() block; not part of any published API.
        seen["before"] = get_originator()
        with invoker.on_own_account():
            seen["during"] = get_originator()
            seen["headers_during"] = r1_client.R1Client(bearer_token="t")._headers()
        seen["after"] = get_originator()
        seen["headers_after"] = r1_client.R1Client(bearer_token="t")._headers()
        return {}

    TestClient(app).get("/inside", headers={**RAPP, "X-R1-Scope": '{"regions":["eu"]}'})
    assert (seen["before"], seen["during"], seen["after"]) == ("es-client", None, "es-client")
    assert ON_BEHALF_OF_HEADER not in seen["headers_during"] and "X-R1-On-Behalf-Scope" not in seen["headers_during"]
    assert seen["headers_after"][ON_BEHALF_OF_HEADER] == "es-client" and seen["headers_after"]["X-R1-On-Behalf-Scope"] == '{"regions":["eu"]}'


def test_leaving_the_own_account_block_by_an_error_restores_the_originator():
    """An exception inside on_own_account() still restores the request's originator afterwards."""
    app = FastAPI()
    apply_correlation_id(app)
    seen = {}

    @app.get("/inside")
    def inside():
        # Test route raising inside an on_own_account() block and recording the originator afterwards; not part of any published API.
        try:
            with invoker.on_own_account():
                raise RuntimeError("boom")
        except RuntimeError:
            seen["after"] = get_originator()
        return {}

    TestClient(app).get("/inside", headers=RAPP)
    assert seen["after"] == "es-client"


def test_the_acting_user_is_believed_only_from_an_internal_caller():
    """SEC-15.8: `acting_user` returns the person the console named (stripped) for the `internal` role, and None for an rApp that sent the header, for a call with no role and for an
    empty or blank value, so a module that reads it cannot be given a person by anyone but an SMO module.
    """
    def request(headers):
        return type("R", (), {"headers": headers})()
    assert acting_user(request({**MODULE, ACTING_USER_HEADER: " smo-gui:alice "})) == "smo-gui:alice"
    assert acting_user(request({**RAPP, ACTING_USER_HEADER: "smo-gui:alice"})) is None
    assert acting_user(request({ACTING_USER_HEADER: "smo-gui:alice"})) is None
    assert acting_user(request({**MODULE, ACTING_USER_HEADER: "   "})) is None
    assert acting_user(request(MODULE)) is None
