"""Tests of call flow 11a (DOC-CF-1.8) on RAN NF OAM's side: a PM or FM subscription and a KPI publication register RAN NF OAM as the DME producer first, and a
refusal from DME, or no answer, is 502 UPSTREAM_FAILED with no subscription stored (the answer used to be ignored).

Fixtures come from `test_main.py` (the app on SQLite and the managed element ME-1). Run: `cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest
tests/test_dme_registration.py -q`.
"""

from types import SimpleNamespace

import httpx
import pytest

from app.models import FMSubscription, PMSubscription
from test_main import client, db_session_factory, elements  # noqa: F401  (pytest fixtures)


@pytest.mark.parametrize("answer", [SimpleNamespace(status_code=403, json=lambda: {}), httpx.ConnectError("refused")])
@pytest.mark.parametrize("path, params, model", [
    ("/pm-subscriptions", {"managed_element_ref": "ME-1", "counter_type": "PRB.Usage", "delivery_method": "pull"}, PMSubscription),
    ("/fm-subscriptions", {"managed_element_ref": "ME-1", "delivery_method": "push"}, FMSubscription),
])
def test_a_subscription_dme_refuses_is_502_and_not_stored(client, db_session_factory, monkeypatch, elements, answer, path, params, model):
    """Call flow 11a: DME refusing RAN NF OAM's producer registration, or not answering, is 502 and the subscription is not stored."""
    def post(self, p, json=None, **kw):
        """DME's scripted answer."""
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr("app.main.R1Client.post", post)
    resp = client.post(path, params=params)
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert db_session_factory().query(model).count() == 0
