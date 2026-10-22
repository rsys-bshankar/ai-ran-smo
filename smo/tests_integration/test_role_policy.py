"""PR-SEC-14: the role policy (`smo_shared/roles.py`) against the apps' real route tables.

The policy is a deny-list of (R1 prefix, methods, path) plus an allow-list of the changes an rApp may make (`RAPP_MAY_CHANGE`); lists like that rot when a route is renamed. So: every entry must match a route that
exists, and a walk of every route of every backend through the real gateway app must show an rApp refused on exactly the routes the policy names and
an SMO module refused on none.
"""

import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from smo_shared import roles

from mesh import R1_PREFIX_TO_SERVICE
from test_authz_walk import backend_routes

TOKENS = {"module-token": "internal", "rapp-token": "rapp"}


class RoleGateway:
    """SME's introspection answers by token; the backends record what reached them."""

    def __init__(self, upstream_calls, **_):
        self.upstream_calls = upstream_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        import httpx
        if urlparse(url).path == "/oauth2/introspect" and "json" in kwargs:
            role = TOKENS.get(kwargs["json"]["token"])
            return httpx.Response(200, json={"active": role is not None, "client_id": f"inv-{role}", "role": role})
        self.upstream_calls.append((method, url))
        return httpx.Response(200, json={})


@pytest.fixture
def gateway(loaded_apps, monkeypatch):
    """A TestClient on the real R1 gateway with the rate limit off and role enforcement left at its default, whose upstream is a fake that accepts
    two tokens (an rApp's and a module's); returns (client, upstream calls).
    """
    upstream_calls: list = []
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")
    monkeypatch.delenv("SMO_ROLE_ENFORCEMENT", raising=False)
    r1 = loaded_apps["r1-termination"]
    monkeypatch.setattr(r1.httpx, "AsyncClient", lambda **kwargs: RoleGateway(upstream_calls, **kwargs))
    return TestClient(r1.app), upstream_calls


def _real_routes(loaded_apps, prefix):
    return list(backend_routes(loaded_apps[R1_PREFIX_TO_SERVICE[prefix]].app))


def test_every_policy_entry_names_a_route_that_exists(loaded_apps):
    """Every internal-only entry of the policy matches a route that exists in its module, so a renamed route cannot leave a rule that guards
    nothing.
    """
    for module, methods, pattern in roles.INTERNAL_ONLY:
        found = [(m, p) for m, p in _real_routes(loaded_apps, module) if m in methods and pattern.match(p)]
        assert found, f"{module} {sorted(methods)} {pattern.pattern} matches no route of {R1_PREFIX_TO_SERVICE[module]}: renamed or removed?"


def test_every_allow_list_rule_names_a_route_that_exists(loaded_apps):
    """Every rule that allows an rApp a change matches a route that exists in its module."""
    for module, rules in roles.RAPP_MAY_CHANGE.items():
        if rules is None:
            continue
        for methods, pattern in rules:
            found = [(m, p) for m, p in _real_routes(loaded_apps, module) if m in methods and pattern.match(p)]
            assert found, f"{module} {sorted(methods)} {pattern.pattern} allows an rApp a route that does not exist in {R1_PREFIX_TO_SERVICE[module]}"


def test_an_rapp_is_refused_on_exactly_the_routes_the_policy_names_and_a_module_on_none(loaded_apps, gateway):
    """A walk of every route of every backend through the real gateway shows an rApp refused (403 ROLE_NOT_PERMITTED) on exactly the routes the
    policy names and an SMO module refused on none.
    """
    client, upstream_calls = gateway
    refused_to_rapp, wrongly_refused_to_module, walked = [], [], 0
    for prefix in sorted(R1_PREFIX_TO_SERVICE):
        for method, path in _real_routes(loaded_apps, prefix):
            walked += 1
            expected = roles.internal_only(prefix, method, path) or not roles.rapp_may_change(prefix, method, path)
            rapp = client.request(method, f"{prefix}{path}", headers={"Authorization": "Bearer rapp-token"})
            module = client.request(method, f"{prefix}{path}", headers={"Authorization": "Bearer module-token"})
            if (rapp.status_code == 403 and rapp.json().get("title") == "ROLE_NOT_PERMITTED") != expected:
                refused_to_rapp.append(f"{method} {prefix}{path}: rApp -> {rapp.status_code}, policy says {expected}")
            if module.status_code == 403:
                wrongly_refused_to_module.append(f"{method} {prefix}{path}")
    assert walked > 400
    assert refused_to_rapp == [] and wrongly_refused_to_module == []


def test_the_policy_covers_what_the_per_rapp_limits_and_the_kpi_definitions_depend_on():
    """The policy keeps rApps from changing the per-rApp limits, the KPI definitions and the config-history purge, which the per-rApp limits and
    the KPI definitions depend on.
    """
    assert roles.internal_only("/ran-nf-oam", "PUT", "/rapp-limits/x")
    assert roles.internal_only("/ran-nf-oam", "PUT", "/kpi-definitions/k")
    assert roles.internal_only("/ran-nf-oam", "POST", "/config-history/purge")
    assert re.compile(r"^/rapp-limits/[^/]+$").match("/rapp-limits/api-invoker-1")
