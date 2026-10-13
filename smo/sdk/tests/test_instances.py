"""`smo_sdk.instances`: an rApp instance's own identity inside the rApp process (docs/call-flows/01-rapp-onboarding-to-deployment.md, 01c and 01d).
The credentials route, the request that acts for its instance, and `bootstrap`. Run with: cd smo/sdk && PYTHONPATH=.:../shared python -m pytest tests -q"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smo_sdk import AiRuntimeSdk, SdkError, instances
from smo_shared import r1_client
from smo_shared.roles import ROLE_HEADER, ROLE_INTERNAL

IID = str(uuid.uuid4())


@pytest.fixture
def app(monkeypatch):
    """A FastAPI app with `instances.install`, a route that reports which instance its calls act for, and an empty identity registry."""
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    application = FastAPI()
    instances.install(application)

    @application.get("/instances/{instance_id}/who")
    def who(instance_id: str):
        """The instance this request's calls act for, and the invoker held for it."""
        return {"acting": r1_client._acting_instance.get(), "invoker": instances.invoker_of(instance_id)}

    @application.get("/other")
    def other():
        """A request outside any instance: acts for none."""
        return {"acting": r1_client._acting_instance.get()}

    return TestClient(application)


def _push(app, role=ROLE_INTERNAL):
    """Pushes credentials for `IID` with the given `X-R1-Role` (none when `role` is None)."""
    return app.post(f"/instances/{IID}/credentials", json={"oauthClientId": "api-invoker-x", "oauthClientSecret": "s"},
                    headers={ROLE_HEADER: role} if role else {})


def test_rapp_management_pushes_the_credentials_and_the_instances_requests_act_for_it(app):
    """A push from an SMO module (the internal role R1 stamps) is kept, and a request under `/instances/{id}/` acts for that instance; other
    requests act for none."""
    assert _push(app).status_code == 204
    assert instances.is_bound(IID) and instances.invoker_of(IID) == "api-invoker-x"
    assert app.get(f"/instances/{IID}/who").json() == {"acting": IID, "invoker": "api-invoker-x"}
    assert app.get("/other").json() == {"acting": None}


def test_a_push_not_from_an_smo_module_is_refused(app):
    """A push without the internal role (an rApp, or a call that did not come through the gateway) is 403 and keeps nothing."""
    assert _push(app, role="rapp").status_code == 403
    assert _push(app, role=None).status_code == 403
    assert not instances.is_bound(IID)


class _Platform:
    """A stand-in for `sdk.platform` whose `bootstrap_complete` answers or raises as told."""

    def __init__(self, outcome):
        """Keeps the outcome to give."""
        self.outcome, self.calls = outcome, []

    def bootstrap_complete(self, instance_id):
        """Records the call, then answers or raises the outcome."""
        self.calls.append(instance_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _sdk(outcome):
    """An SDK whose `platform` is a `_Platform` with `outcome`."""
    sdk = AiRuntimeSdk(r1=object())
    sdk.platform = _Platform(outcome)
    return sdk


def test_bootstrap_reports_the_instance_up_only_with_its_credentials(app):
    """Without credentials nothing is called and the answer says why; with them the instance's state comes back, and a refusal is reported with
    its status and title instead of raising."""
    sdk = _sdk({"state": "RUNNING"})
    assert instances.bootstrap(sdk, IID)["reason"].startswith("NO_CREDENTIALS") and sdk.platform.calls == []
    _push(app)
    assert instances.bootstrap(sdk, IID) == {"state": "RUNNING"} and sdk.platform.calls == [IID]
    refused = _sdk(SdkError(409, {"title": "ILLEGAL_STATE_TRANSITION"}))
    assert instances.bootstrap(refused, IID) == {"state": None, "reason": "409 ILLEGAL_STATE_TRANSITION"}


def test_bootstrap_complete_posts_to_rapp_management(r1):
    """`sdk.platform.bootstrap_complete` is one POST to rApp Management's bootstrap-complete of the instance."""
    AiRuntimeSdk(r1).platform.bootstrap_complete(IID)
    assert (r1.calls[-1]["verb"], r1.calls[-1]["path"]) == ("post", f"/rapp-mgmt/instances/{IID}/bootstrap-complete")
