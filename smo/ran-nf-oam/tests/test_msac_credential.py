"""SEC-15.6: an MSAC Identity's credential is verified, in constant time, when an rApp names that Identity in `requestedBy` of `POST /config-jobs`.

Fixtures and helpers come from `test_main.py` (`client`, `db_session_factory`, `_make_me`) and `test_spec_conformance.py` (`applied`, `_rule`, `_role`, `_identity`, `_change`). The gateway's
headers are sent by hand (`X-R1-Role`, `X-R1-Invoker-Id`). SQLite, no network. Run with
`cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/test_msac_credential.py -q`.
"""

import hmac

import pytest

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_spec_conformance import _change, _identity, _role, _rule, applied  # noqa: F401  (fixture and helpers)

from app import msac

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
INTERNAL = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "smo-gui"}


def _alice(client, db_session_factory, credential="s3cret"):
    """An element `ME-1` and an Identity `alice` that may update its cells, with the given credential (None: no credential)."""
    _make_me(db_session_factory)
    allow = _rule(client, "/ManagedElement=ME-1/NRCellDU=*", ["update"])
    if credential is None:
        client.post("/msac/identities", json={"identityType": "USERNAME", "identityName": "alice", "roleList": [_role(client, "cells", [allow])]})
    else:
        _identity(client, "alice", [_role(client, "cells", [allow])], credential=credential)


def _write(client, headers):
    """POST a one-cell config job as `alice` with the given headers."""
    return client.post("/config-jobs", json={"requestedBy": "alice", "accessScope": "cell", "changes": [_change()]}, headers=headers)


def test_an_rapp_naming_an_identity_with_a_credential_must_present_it(client, db_session_factory, applied):
    """Without the header, or with a wrong one, an rApp is 403 `MSAC_ACCESS_DENIED` and nothing is dispatched or recorded; the right one writes (the finding: `requestedBy` alone was enough)."""
    _alice(client, db_session_factory)
    for headers in (RAPP, {**RAPP, msac.CREDENTIAL_HEADER: "wrong"}, {**RAPP, msac.CREDENTIAL_HEADER: ""}):
        resp = _write(client, headers)
        assert resp.status_code == 403 and "MSAC_ACCESS_DENIED" in resp.text and "s3cret" not in resp.text
    assert applied == [] and client.get("/config-jobs").json()["items"] == []
    assert _write(client, {**RAPP, msac.CREDENTIAL_HEADER: "s3cret"}).status_code == 202 and applied


def test_an_identity_without_a_credential_is_not_asked_for_one(client, db_session_factory, applied):
    """Fail closed only where a credential was configured: an Identity made with none, and a requester that is no Identity, write as before from an rApp."""
    _alice(client, db_session_factory, credential=None)
    assert _write(client, RAPP).status_code == 202
    other = client.post("/config-jobs", json={"requestedBy": "unregistered", "accessScope": "cell", "msacRole": "cells", "changes": [_change()]}, headers=RAPP)
    assert other.status_code == 202


def test_the_console_and_a_call_without_a_role_are_not_asked_for_the_credential(client, db_session_factory, applied):
    """The console and SMO modules (role internal) authenticate a person themselves, and a call with no role did not come through the gateway: neither is asked, as before."""
    _alice(client, db_session_factory)
    assert _write(client, INTERNAL).status_code == 202
    assert _write(client, {}).status_code == 202


def test_a_dry_run_is_checked_too(client, db_session_factory, applied):
    """A dry run reveals what the Identity may do, so it needs the credential as a write does."""
    _alice(client, db_session_factory)
    body = {"requestedBy": "alice", "accessScope": "cell", "changes": [_change()], "dryRun": True}
    assert client.post("/config-jobs", json=body, headers=RAPP).status_code == 403
    assert client.post("/config-jobs", json=body, headers={**RAPP, msac.CREDENTIAL_HEADER: "s3cret"}).status_code == 200


def test_check_credential_uses_a_constant_time_comparison(monkeypatch):
    """`check_credential` compares the digests with `hmac.compare_digest` (a `==` leaks how long the matching prefix is), and still gives the right answer."""
    stored = msac.hash_credential("hunter2")
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(msac.hmac, "compare_digest", lambda a, b: calls.append((type(a), type(b))) or real(a, b))
    assert msac.check_credential("hunter2", stored) is True
    assert msac.check_credential("hunter3", stored) is False
    assert calls == [(bytes, bytes), (bytes, bytes)]


@pytest.mark.parametrize("stored", [None, "", "plain", "scrypt$zz$zz", "scrypt$00", "md5$00$00", "scrypt$00$00$00"])
def test_a_missing_or_damaged_stored_hash_refuses_without_an_error(stored):
    """Nothing stored, or something that is not `scrypt$<hex>$<hex>`, is a False and never an exception (a damaged row must not turn into a 500 or a pass)."""
    assert msac.check_credential("anything", stored) is False
