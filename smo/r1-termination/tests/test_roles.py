"""PR-SEC-14: the role policy at the gateway: an rApp is refused on the internal-only routes, an SMO module is not, and what a backend reads as the caller's role is only ever what SME said.

Covers the role step of `_proxy` against the lists in `smo_shared/roles.py` (the deny-list `INTERNAL_ONLY` and the change allow-list `RAPP_MAY_CHANGE`), the enforce/audit
modes, how a role is derived from SME's answer, and the `X-R1-On-Behalf-Of` rule. A fake SME and backend replace httpx (`gateway` fixture). A route added to either list
in `roles.py` should be added to the tables here. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_roles.py -q`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from smo_shared import roles

from app.main import ROUTES, app

client = TestClient(app)
AUTH = {"Authorization": "Bearer t"}
INTROSPECT_URL = f"{ROUTES['/sme']}/oauth2/introspect"


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """Autouse fixture: every test starts and ends with every caller's rate bucket full."""
    from app.main import _limiter
    _limiter.clear()
    yield
    _limiter.clear()


class FakeResponse:
    def __init__(self, content=b"{}", status_code=200):
        self.content, self.status_code, self.headers = content, status_code, {}

    def json(self):
        return json.loads(self.content)


@pytest.fixture
def gateway(monkeypatch):
    """SME says what `sme_says` holds; backends record what they were sent."""
    state = {"sme_says": {"active": True, "client_id": "inv-1", "role": "rapp"}, "forwarded": []}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            if url == INTROSPECT_URL:
                return FakeResponse(json.dumps(state["sme_says"]).encode())
            state["forwarded"].append((method, url, kwargs.get("headers", {})))
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.delenv("SMO_ROLE_ENFORCEMENT", raising=False)
    return state


INTERNAL_ONLY_CALLS = [("PUT", "/ran-nf-oam/rapp-limits/x"), ("DELETE", "/ran-nf-oam/rapp-limits/x"), ("PUT", "/ran-nf-oam/kpi-definitions/k"),
                       ("DELETE", "/ran-nf-oam/kpi-definitions/k"), ("POST", "/ran-nf-oam/config-history/purge"), ("PUT", "/ran-nf-oam/rapp-kill/x"),
                       ("DELETE", "/ran-nf-oam/rapp-kill/x"), ("GET", "/ran-nf-oam/rapp-kill"), ("PUT", "/rapp-mgmt/instances/i/kill"),
                       ("DELETE", "/rapp-mgmt/instances/i/kill"), ("PUT", "/rapp-mgmt/kill-all"), ("DELETE", "/rapp-mgmt/kill-all"), ("GET", "/rapp-mgmt/kill-all"),
                       ("POST", "/ran-nf-oam/kpi-definitions/standard"), ("POST", "/ran-nf-oam/kpis/k/publish"), ("GET", "/ran-nf-oam/safeguard-refusals"), ("GET", "/ran-nf-oam/safeguard-subscriptions"),
                       ("POST", "/ran-nf-oam/safeguard-subscriptions"), ("DELETE", "/ran-nf-oam/safeguard-subscriptions/s"),
                       # AI-11 / AI-13: who decides, who must be asked, and the lists that name other rApps' actions
                       ("PUT", "/ran-nf-oam/rapp-approval-policy/x"), ("DELETE", "/ran-nf-oam/rapp-approval-policy/x"), ("POST", "/ran-nf-oam/rapp-approvals/a/approve"),
                       ("POST", "/ran-nf-oam/rapp-approvals/a/reject"), ("POST", "/ran-nf-oam/rapp-approvals/expire-due"), ("GET", "/ran-nf-oam/rapp-approvals"),
                       ("GET", "/ran-nf-oam/approval-subscriptions"), ("POST", "/ran-nf-oam/approval-subscriptions"), ("DELETE", "/ran-nf-oam/approval-subscriptions/s"),
                       ("GET", "/ran-nf-oam/decision-records"), ("GET", "/ran-nf-oam/decision-records/export.csv"),
                       # MGT-14.7 / MGT-15.6: where the platform calls when an onboarding fails or a campaign halts
                       ("GET", "/ran-nf-oam/lifecycle-subscriptions"), ("POST", "/ran-nf-oam/lifecycle-subscriptions"), ("DELETE", "/ran-nf-oam/lifecycle-subscriptions/s"),
                       # SEC-10: the scope of a caller and of a target is set by the platform
                       ("PUT", "/sme/invoker-registrations/i/authz-scope"), ("PUT", "/ran-nf-oam/managed-entities/e/scope"),
                       # MSG-3.2: the SMO's event feed names every module's entities
                       ("GET", "/dme/smo-events")]


@pytest.mark.parametrize("method, path", INTERNAL_ONLY_CALLS)
def test_an_rapp_is_refused_on_an_internal_only_route_and_the_backend_is_never_called(gateway, method, path):
    """Every route in `INTERNAL_ONLY_CALLS` is a 403 `ROLE_NOT_PERMITTED` for an rApp, and no backend sees the call."""
    resp = client.request(method, path, headers=AUTH)
    assert resp.status_code == 403 and resp.json()["title"] == "ROLE_NOT_PERMITTED"
    assert gateway["forwarded"] == []


@pytest.mark.parametrize("method, path", INTERNAL_ONLY_CALLS)
def test_an_smo_module_is_not_refused_there(gateway, method, path):
    """The same calls pass for an `internal` caller and are forwarded once each."""
    gateway["sme_says"]["role"] = "internal"
    assert client.request(method, path, headers=AUTH).status_code == 200
    assert len(gateway["forwarded"]) == 1


@pytest.mark.parametrize("method, path", [("GET", "/ran-nf-oam/rapp-limits/x"), ("GET", "/ran-nf-oam/rapp-kill/x"), ("GET", "/ran-nf-oam/kpi-definitions"), ("GET", "/ran-nf-oam/kpi-definitions/standard"),
                                          ("GET", "/ran-nf-oam/rapp-approvals/a"), ("GET", "/ran-nf-oam/decision-records/d"), ("GET", "/ran-nf-oam/rapp-approval-policy/x"), ("GET", "/ran-nf-oam/kpis/k"),
                                          ("POST", "/ran-nf-oam/config-jobs"), ("POST", "/ran-nf-oam/config-jobs/j/rollback"), ("POST", "/dme/actions"), ("POST", "/aimgf/training-jobs"),
                                          # the rApp container reports that it is up and how it performs (rApp Management checks that the instance is its own)
                                          ("POST", "/rapp-mgmt/instances/i/bootstrap-complete"), ("POST", "/rapp-mgmt/instances/i/performance"),
                                          ("DELETE", "/sme/provider-registrations/a"), ("GET", "/onboarding/packages"), ("GET", "/rapp-mgmt/instances"), ("GET", "/sme/trusted-invokers")])
def test_other_routes_are_open_to_an_rapp_as_before(gateway, method, path):
    """Routes that look similar to internal-only ones but are not (reads of one's own record, a different method or path) stay open to an rApp."""
    assert client.request(method, path, headers=AUTH).status_code == 200


def test_audit_mode_lets_the_call_through(gateway, monkeypatch):
    """With `SMO_ROLE_ENFORCEMENT=audit` the call that would be refused is forwarded (the decision is counted and logged only)."""
    monkeypatch.setenv("SMO_ROLE_ENFORCEMENT", "audit")
    assert client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH).status_code == 200
    assert len(gateway["forwarded"]) == 1


def test_a_mistyped_mode_still_enforces(gateway, monkeypatch):
    """Any `SMO_ROLE_ENFORCEMENT` value other than `audit` enforces, so a typo cannot switch the policy off."""
    monkeypatch.setenv("SMO_ROLE_ENFORCEMENT", "of")
    assert client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH).status_code == 403


def test_the_role_a_backend_sees_is_the_one_sme_gave_never_the_callers(gateway):
    """The role and invoker-id headers a caller sends are replaced by the values from SME's introspection before forwarding."""
    client.get("/ran-nf-oam/health", headers={**AUTH, roles.ROLE_HEADER: "internal", "X-R1-Invoker-Id": "someone-else"})
    headers = {k.lower(): v for k, v in gateway["forwarded"][0][2].items()}
    assert headers[roles.ROLE_HEADER.lower()] == "rapp" and headers["x-r1-invoker-id"] == "inv-1"


def test_an_sme_that_does_not_say_is_read_by_the_scope(gateway):
    """When SME's answer has no `role`, the scope decides: `smo-internal` is a module, any other scope or none is an rApp."""
    gateway["sme_says"] = {"active": True, "client_id": "old", "scope": "smo-internal"}
    assert client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH).status_code == 200
    gateway["sme_says"] = {"active": True, "client_id": "old", "scope": "3gpp#aef:api"}
    assert client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH).status_code == 403
    gateway["sme_says"] = {"active": True, "client_id": "old"}
    assert client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH).status_code == 403            # no role, no scope: an rApp


def test_refusals_are_counted(gateway):
    """A role refusal increments `smo_role_refusals_total` for the module and `refused` action."""
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value("smo_role_refusals_total", {"module": "ran-nf-oam", "action": "refused"}) or 0
    client.put("/ran-nf-oam/rapp-limits/x", headers=AUTH)
    assert REGISTRY.get_sample_value("smo_role_refusals_total", {"module": "ran-nf-oam", "action": "refused"}) == before + 1


def _forwarded(gateway):
    """The headers of the first forwarded call, lower-cased."""
    return {k.lower(): v for k, v in gateway["forwarded"][0][2].items()}


def test_an_internal_module_may_say_whom_it_acts_for(gateway):
    """`X-R1-On-Behalf-Of` from an `internal` caller is forwarded next to the module's own invoker id, so the rApp's safeguards apply to what the module writes."""
    gateway["sme_says"] = {"active": True, "client_id": "dme-client", "role": "internal"}
    client.get("/ran-nf-oam/health", headers={**AUTH, "X-R1-On-Behalf-Of": "es-client"})
    headers = _forwarded(gateway)
    assert headers["x-r1-on-behalf-of"] == "es-client" and headers["x-r1-invoker-id"] == "dme-client" and headers[roles.ROLE_HEADER.lower()] == "internal"


def test_an_rapp_cannot_pose_as_another_rapp(gateway):
    """An rApp's own `X-R1-On-Behalf-Of` is dropped, and the invoker id forwarded stays its own."""
    client.get("/ran-nf-oam/health", headers={**AUTH, "X-R1-On-Behalf-Of": "someone-else"})
    headers = _forwarded(gateway)
    assert "x-r1-on-behalf-of" not in headers and headers["x-r1-invoker-id"] == "inv-1"


def test_a_module_that_names_nobody_forwards_nothing(gateway):
    """A module that does not send the header gets none added."""
    gateway["sme_says"] = {"active": True, "client_id": "dme-client", "role": "internal"}
    client.get("/ran-nf-oam/health", headers=AUTH)
    assert "x-r1-on-behalf-of" not in _forwarded(gateway)


# --- the rApp change allow-list -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("POST", "/onboarding/packages"), ("DELETE", "/onboarding/packages/p"), ("POST", "/rapp-mgmt/instances"), ("POST", "/rapp-mgmt/instances/i/terminate"),
    ("POST", "/nfo/deployments"), ("POST", "/focom/provisioning-requests"), ("POST", "/so-smos/anything"), ("POST", "/rapps/00000000-0000-0000-0000-000000000000/operator/instances/i/start"),
    ("PUT", "/sme/invoker-registrations/x"), ("DELETE", "/sme/invoker-registrations/x"), ("POST", "/sme/invoker-registrations/purge-stale"),
    ("PUT", "/sme/trusted-invokers/x"), ("POST", "/aimgf/ml-training-functions"), ("POST", "/aimgf/execution-timeouts/sweep"),
    ("POST", "/aimgf/models/m/runtime/terminate"),
    # SEC-15.1b: the governance and end-of-life decisions of a model are the operator's; the route is refused to an rApp at the gateway (and by AIMgF), with the near misses of its neighbours
    ("POST", "/aimgf/models/m/advance"), ("POST", "/aimgf/models/m/advance/x"), ("PUT", "/aimgf/models/m/advance"), ("POST", "/aimgf/models/advance"),
    ("POST", "/aimgf/models/m/inference-jobs/x"), ("POST", "/aimgf/models/m/runtime/deploy/x"), ("DELETE", "/aimgf/models/m/runtime/activate"),
    ("POST", "/mlmr/storages"), ("POST", "/mdaf/mda-functions"), ("PUT", "/ran-nf-oam/rapp-limits/x"),
    ("POST", "/ran-nf-oam/config-jobs/j/abort"), ("PUT", "/ran-nf-oam/kpi-schedules/s"), ("POST", "/ran-nf-oam/managed-elements"),
    # GUI-8.3: an rApp registers (and forgets) the operator API of an instance, with those two methods and that one path
    ("POST", "/rapp-mgmt/instances/i/operator-api"), ("PATCH", "/rapp-mgmt/instances/i/operator-api"), ("PUT", "/rapp-mgmt/instances/i/operator-api/x"),
    ("PUT", "/rapp-mgmt/instances/i/j/operator-api"), ("DELETE", "/rapp-mgmt/instances/operator-api"),
    # an rApp reports its own bootstrap and performance, and nothing else of its instance's life cycle: not those routes with another method, path or id shape
    ("PUT", "/rapp-mgmt/instances/i/bootstrap-complete"), ("DELETE", "/rapp-mgmt/instances/i/performance"), ("PATCH", "/rapp-mgmt/instances/i/performance"),
    ("POST", "/rapp-mgmt/instances/i/bootstrap-complete/x"), ("POST", "/rapp-mgmt/instances/i/j/performance"), ("POST", "/rapp-mgmt/instances/bootstrap-complete"),
    ("POST", "/rapp-mgmt/x/i/performance"),
    ("POST", "/rapp-mgmt/instances/i/fault"), ("POST", "/rapp-mgmt/instances/i/recover"), ("POST", "/rapp-mgmt/instances/i/credentials"),
    ("POST", "/rapp-mgmt/instances/i/upgrade"), ("PUT", "/rapp-mgmt/instances/i/config"), ("POST", "/rapp-mgmt/instances/i/performance-x"),
])
def test_an_rapp_may_not_change_what_it_does_not_use(gateway, method, path):
    """A change by an rApp outside `RAPP_MAY_CHANGE` is a 403 `ROLE_NOT_PERMITTED` with no backend call; the table includes near misses (another method, an extra segment, another id shape) that must also be refused."""
    resp = client.request(method, path, headers=AUTH)
    assert resp.status_code == 403 and resp.json()["title"] == "ROLE_NOT_PERMITTED"
    assert gateway["forwarded"] == []


@pytest.mark.parametrize("method, path", [
    ("POST", "/ran-nf-oam/config-jobs"), ("POST", "/dme/actions"), ("PUT", "/dme/data-jobs/j"),
    ("POST", "/aimgf/models/m/inference-jobs"), ("POST", "/aimgf/models/m/runtime/deploy"), ("POST", "/aimgf/models/m/runtime/activate"),
    ("POST", "/aimgf/ml-training-requests"), ("PATCH", "/intent-service/intents/i/admin-state"), ("POST", "/sme/oauth2/token"),
    ("POST", "/mlmr/models/m/artifact"), ("DELETE", "/mdaf/subscriptions/s"), ("PUT", "/rapp-mgmt/instances/i/operator-api"), ("DELETE", "/rapp-mgmt/instances/i/operator-api"),
    ("POST", "/rapp-mgmt/instances/i/bootstrap-complete"), ("POST", "/rapp-mgmt/instances/i/performance"),
])
def test_an_rapp_may_change_what_it_uses(gateway, method, path):
    """The changes the SDK and the 3GPP consumer routes need are open to an rApp."""
    assert client.request(method, path, headers=AUTH).status_code == 200


def test_every_change_is_decided_for_an_internal_module_too_and_it_is_never_refused(gateway):
    """The allow-list does not apply to an `internal` caller: it may change any module."""
    gateway["sme_says"]["role"] = roles.ROLE_INTERNAL
    assert client.post("/onboarding/packages", headers=AUTH).status_code == 200


def test_audit_mode_lets_an_unlisted_change_through(gateway, monkeypatch):
    """In audit mode a change outside the allow-list is forwarded."""
    monkeypatch.setenv("SMO_ROLE_ENFORCEMENT", "audit")
    assert client.post("/onboarding/packages", headers=AUTH).status_code == 200


def test_the_allow_list_names_only_modules_the_gateway_routes_to():
    """Every module prefix in `RAPP_MAY_CHANGE` is in the gateway's route table, so an entry cannot name a module that does not exist."""
    from app.main import ROUTES
    assert set(roles.RAPP_MAY_CHANGE) <= set(ROUTES)
