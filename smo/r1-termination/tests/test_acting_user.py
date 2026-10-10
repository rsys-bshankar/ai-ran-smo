"""SEC-15.8: the gateway forwards `X-R1-Acting-User` (the person the operator's console acts for) from an `internal` caller only.

The header is how a module learns who decided an approval without believing the request body. The gateway drops every inbound value and forwards the console's own, so a
backend that reads it in an `internal` request is reading the console's word. The fake SME and backend (`gateway` fixture) and the `_forwarded` helper come from `test_roles.py`.
Run: `PYTHONPATH=.:../shared python -m pytest tests/test_acting_user.py -q`.
"""

from test_roles import AUTH, client, fresh_rate_limiter, gateway, _forwarded  # noqa: F401  (fixtures and helpers)


def test_the_console_may_say_whom_it_acts_for(gateway):
    """`X-R1-Acting-User` from an `internal` caller reaches the backend unchanged, next to the console's own invoker id and role."""
    gateway["sme_says"] = {"active": True, "client_id": "gui-invoker", "role": "internal"}
    client.post("/ran-nf-oam/rapp-approvals/a/approve", headers={**AUTH, "X-R1-Acting-User": "smo-gui:alice"}, json={})
    headers = _forwarded(gateway)
    assert headers["x-r1-acting-user"] == "smo-gui:alice" and headers["x-r1-invoker-id"] == "gui-invoker" and headers["x-r1-role"] == "internal"


def test_an_rapp_cannot_name_a_person(gateway):
    """An rApp's own `X-R1-Acting-User` is dropped, whatever the path, and the role forwarded stays `rapp`."""
    client.get("/ran-nf-oam/health", headers={**AUTH, "X-R1-Acting-User": "smo-gui:alice"})
    headers = _forwarded(gateway)
    assert "x-r1-acting-user" not in headers and headers["x-r1-role"] == "rapp"


def test_a_caller_that_names_nobody_forwards_nothing(gateway):
    """An `internal` caller that sends no header gets none added."""
    gateway["sme_says"] = {"active": True, "client_id": "dme-client", "role": "internal"}
    client.get("/ran-nf-oam/health", headers=AUTH)
    assert "x-r1-acting-user" not in _forwarded(gateway)
