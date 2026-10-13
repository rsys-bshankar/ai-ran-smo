"""SEC-15.6b, end to end: an rApp that writes through DME with an MSAC Identity that has a credential must present it, and DME passes it on.

DME's `POST /actions` takes `X-MSAC-Credential` and forwards it to RAN NF OAM's `POST /config-jobs`, which (seen from there the call is an SMO module's, with `X-R1-On-Behalf-Of` naming the
rApp) asks for it. The in-process mesh has no gateway, so the rApp's headers are sent by hand, as `test_on_behalf_of.py` does. The credential is a made-up string.
"""

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
SECRET = "s3cret-for-alice-0042"
HEADER = "X-MSAC-Credential"


def _identity(mesh, credential=SECRET):
    """An Identity `alice` that may update every cell of ME-1, with the given credential (None: none)."""
    oam = mesh["ran-nf-oam"]
    rule = oam.post("/msac/access-rules", json={"ruleName": "r", "dataNodeSelector": "/ManagedElement=ME-1/*", "operations": ["update"], "actions": "ALLOW"}).json()["id"]
    role = oam.post("/msac/roles", json={"roleName": "cells", "accessRulesList": [rule]}).json()["id"]
    body = {"identityType": "USERNAME", "identityName": "alice", "roleList": [role]}
    assert oam.post("/msac/identities", json={**body, **({"credential": credential} if credential else {})}).status_code == 201


def _action(mesh, headers):
    """An action by `alice` through DME from the rApp, with the given headers."""
    return mesh["dme"].post("/actions", headers={**RAPP, **headers}, json={"requestedBy": "alice", "scope": "cell", "changes": [
        {"managedElementRef": "ME-1", "attributeChanges": {"txPower": 12}}]})


def _asked_for_the_credential(response):
    """Whether the answer is RAN NF OAM's refusal for a missing or wrong credential (not some other refusal of the same code)."""
    return response.status_code == 403 and "send it in the X-MSAC-Credential header" in response.text


def test_a_credentialed_identity_through_dme_needs_the_credential(mesh):
    """Without the header, with a wrong one and with an empty one the action is refused for the credential, DME records it REJECTED and the value is nowhere in an answer or a record."""
    _identity(mesh)
    for headers in ({}, {HEADER: "wrong-0042"}, {HEADER: ""}):
        resp = _action(mesh, headers)
        assert _asked_for_the_credential(resp), resp.text
        assert "wrong-0042" not in resp.text and SECRET not in resp.text
    assert [a["status"] for a in mesh["dme"].get("/actions").json()["items"]] == ["REJECTED"] * 3


def test_the_right_credential_passes_through_dme_and_is_never_stored_or_returned(mesh):
    """With the right credential the credential check passes (the action goes on to the checks after it, whatever they answer) and the credential is not in the answer, the action record or the list."""
    _identity(mesh)
    resp = _action(mesh, {HEADER: SECRET})
    assert not _asked_for_the_credential(resp), resp.text
    assert SECRET not in resp.text
    listing = mesh["dme"].get("/actions")
    assert SECRET not in listing.text and SECRET not in mesh["ran-nf-oam"].get("/config-jobs").text


def test_an_identity_without_a_credential_and_no_identity_are_not_asked_through_dme(mesh):
    """Nothing changes where no credential was configured: an Identity made with none, and a requester that is no Identity."""
    _identity(mesh, credential=None)
    assert not _asked_for_the_credential(_action(mesh, {}))
    other = mesh["dme"].post("/actions", headers=RAPP, json={"requestedBy": "nobody", "scope": "cell", "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 1}}]})
    assert not _asked_for_the_credential(other)
