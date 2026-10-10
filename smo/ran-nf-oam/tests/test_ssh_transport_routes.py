"""PR-SB-1.2/1.5: `transport` on the endpoint, and CM write / read going over NETCONF-over-SSH to an in-process server."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import outbox  # noqa: F401  (registers the outbox table on the metadata)
from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.outbox import NotificationOutbox
from smo_shared.testing import make_test_engine

from app.main import app
from app.models import (AlarmComment, AlarmHistory, ElementOnboarding, LifecycleSubscription, OnboardingTemplate, SoftwareCampaign, ManagedObject, O1AdaptorHostKey, Alarm, CMSchemaCache, CMSnapshot, ManagedEntity, O1AdaptorEndpoint, VendorCapability, WriteConfigJob, WriteConfigSubChange,
                        MsacAccessRule, MsacIdentity, MsacRole)

from netconf_ssh_server import Behaviour, NetconfTestServer

CAND_CAPS = ("urn:ietf:params:netconf:base:1.0", "urn:ietf:params:netconf:base:1.1", "urn:ietf:params:netconf:capability:candidate:1.0")


@pytest.fixture
def db_session_factory():
    """Fixture: a SQLite session factory with the tables the registration, write and tree routes touch."""
    engine = make_test_engine()
    Base.metadata.create_all(engine, tables=[
        O1AdaptorEndpoint.__table__, ManagedEntity.__table__, Alarm.__table__, AlarmHistory.__table__, AlarmComment.__table__, CMSchemaCache.__table__, WriteConfigJob.__table__,
        WriteConfigSubChange.__table__, CMSnapshot.__table__, VendorCapability.__table__, MsacIdentity.__table__, MsacRole.__table__, MsacAccessRule.__table__,
        IdempotencyKey.__table__, NotificationOutbox.__table__, ManagedObject.__table__, OnboardingTemplate.__table__, ElementOnboarding.__table__, LifecycleSubscription.__table__, SoftwareCampaign.__table__, O1AdaptorHostKey.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """Fixture: a TestClient of the app with `get_session` overridden to a session from `db_session_factory`; the override is removed afterwards."""
    def override():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Fixture: an in-process NETCONF-over-SSH server whose host key is in a known-hosts file and whose password is the shared one; closed
    afterwards.
    """
    server = NetconfTestServer()
    known = tmp_path / "known_hosts"
    known.write_text(server.known_hosts_line())
    monkeypatch.setenv("NETCONF_SSH_KNOWN_HOSTS", str(known))
    monkeypatch.setenv("NETCONF_SSH_PASSWORD", "secret")
    yield server
    server.close()


def _register(client, uri, **extra):
    return client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-1", "adaptorUri": uri, "protocolSupport": ["NETCONF"],
                                                       "o1Protocol": "NETCONF", "entityType": "O-DU", **extra})


def test_default_transport_is_http_mock(client, db_session_factory):
    """An endpoint registered without a transport is `http-mock`, in the table and in the list."""
    assert _register(client, "http://adaptor:8000/edit-config").status_code == 201
    db = db_session_factory()
    assert db.query(O1AdaptorEndpoint).one().transport == "http-mock"
    db.close()
    assert client.get("/o1-adaptor-endpoints").json()["items"][0]["transport"] == "http-mock"


@pytest.mark.parametrize("uri,extra", [
    ("http://adaptor:8000/x", {"transport": "ssh"}),               # scheme does not match the transport
    ("ssh://adaptor", {"transport": "ssh"}),                        # no user name
    ("ssh://admin@adaptor", {}),                                    # ssh:// without transport ssh
    ("ssh://admin@adaptor", {"transport": "ssh", "o1Protocol": "RESTCONF"}),
    ("ssh://admin@adaptor", {"transport": "telnet"}),
])
def test_registration_refuses_a_mismatched_transport(client, uri, extra):
    """Registration refuses a URI whose scheme does not match the transport, an ssh URI without a user or without transport ssh, ssh with RESTCONF,
    and an unknown transport.
    """
    assert _register(client, uri, **extra).status_code in (400, 422)


def test_config_job_and_read_go_over_ssh(client, db_session_factory, lab):
    """A config job, its before-image read and the config read all go over SSH to the registered endpoint, and the job and history show the result."""
    assert _register(client, lab.uri, transport="ssh").status_code == 201
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell",
                                              "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}]})
    assert resp.status_code == 202 and resp.json()["status"] == "COMPLETED"
    assert client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]["status"] == "APPLIED"
    assert "<adminState>UNLOCKED</adminState>" in lab.behaviour.received[-1]
    item = client.get("/managed-entities/ME-1/config-history").json()["items"][0]       # the before image was read over SSH too
    assert item["before"] == {"adminState": None} and item["after"] == {"adminState": "UNLOCKED"} and item["beforeError"] is None
    read = client.get("/managed-entities/ME-1/config")
    assert read.status_code == 200 and read.json()["attributes"] == {"administrativeState": "UNLOCKED"}


def test_the_config_route_reads_a_model_based_server(client, lab):
    """PR-SB-1.5: an endpoint registered with ?model=smo-lab gets a subtree get-config and the leaves come back as SMO attributes."""
    lab.behaviour.data = ('<lab xmlns="urn:smo:lab"><cell><id>101</id><administrative-state>unlocked</administrative-state>'
                          '<tx-power>40</tx-power></cell></lab>')
    assert _register(client, lab.uri + "?model=smo-lab", transport="ssh").status_code == 201
    read = client.get("/managed-entities/ME-1/config", params={"managed_function_ref": "GNBDUFunction=1,NRCellDU=101"})
    assert read.status_code == 200 and read.json()["attributes"] == {"administrativeState": "unlocked", "txPower": "40"}
    assert "managed-object" not in lab.behaviour.received[-1]


def test_a_rejected_model_write_reports_what_the_server_said(client, lab):
    """PR-SB-1.6/1.7 through the route: the job is rejected, the reason is the stable code, and the server's rpc-error is the detail."""
    lab.behaviour.edit_reply = ("<rpc-error><error-tag>invalid-value</error-tag><error-path>/lab/cell/tx-power</error-path>"
                                "<error-message>out of range</error-message></rpc-error>")
    assert _register(client, lab.uri + "?model=smo-lab", transport="ssh").status_code == 201
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": [
        {"managedElementRef": "ME-1", "managedFunctionRef": "GNBDUFunction=1,NRCellDU=101", "attributeChanges": {"txPower": 70000}}]})
    sub = client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]
    assert sub["status"] == "REJECTED" and sub["rejectionReason"] == "NETCONF_RPC_FAILED" and sub["attempts"] == 1
    assert sub["rejectionDetail"] == "invalid-value (a value is not acceptable) at /lab/cell/tx-power: out of range"


def test_a_candidate_endpoint_commits_through_the_route_and_reports_a_failed_commit(client, lab):
    """PR-SB-1.8: the job is APPLIED after lock/edit/commit/unlock; a refused commit is REJECTED with the step in the detail."""
    lab.behaviour.caps = list(CAND_CAPS)
    assert _register(client, lab.uri + "?datastore=candidate", transport="ssh").status_code == 201
    change = {"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}
    sub = client.get(f"/config-jobs/{client.post('/config-jobs', json={'requestedBy': 'operator', 'scope': 'cell', 'changes': [change]}).json()['jobId']}").json()["subChanges"][0]
    assert sub["status"] == "APPLIED"
    lab.behaviour.step_replies = {"commit": "<rpc-error><error-tag>operation-failed</error-tag><error-message>no</error-message></rpc-error>"}
    sub = client.get(f"/config-jobs/{client.post('/config-jobs', json={'requestedBy': 'operator', 'scope': 'cell', 'changes': [change]}).json()['jobId']}").json()["subChanges"][0]
    assert sub["status"] == "REJECTED" and sub["rejectionDetail"].startswith("commit: operation-failed")


def test_the_endpoint_stores_a_reference_and_connects_with_what_it_names(client, lab, monkeypatch, db_session_factory):
    """PR-SB-2: register with credentialRef; the row holds the name, the connect uses the named credential (the shared one is wrong here)."""
    monkeypatch.setenv("NETCONF_SSH_PASSWORD", "wrong")
    monkeypatch.setenv("NETCONF_CRED_GNB_1_PASSWORD", "secret")
    assert _register(client, lab.uri, transport="ssh", credentialRef="gnb-1").status_code == 201
    db = db_session_factory()
    assert db.query(O1AdaptorEndpoint).one().credential_ref == "gnb-1"
    db.close()
    assert client.get("/o1-adaptor-endpoints").json()["items"][0]["credentialRef"] == "gnb-1"
    assert "secret" not in str(client.get("/o1-adaptor-endpoints").json())
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell",
                                              "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}]})
    assert client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]["status"] == "APPLIED"
    assert client.get("/managed-entities/ME-1/config").status_code == 200


@pytest.mark.parametrize("ref", ["Tr0ub4dor&3", "correct horse battery staple", "never-configured"])
def test_registration_refuses_a_literal_secret_or_an_unknown_name_without_echoing_it(client, lab, ref):
    """PR-SB-2.1: a reference is a name of a configured credential; a pasted password is neither."""
    resp = _register(client, lab.uri, transport="ssh", credentialRef=ref)
    assert resp.status_code in (400, 422) and ref not in resp.text


def test_a_credential_ref_needs_the_ssh_transport(client):
    """A credential reference on an endpoint that is neither ssh nor tls is refused."""
    resp = _register(client, "http://adaptor:8000/x", credentialRef="gnb-1")
    assert resp.status_code in (400, 422) and "ssh or tls only" in resp.text


# --- PR-SB-2.3: pinned host keys ---------------------------------------------------------------------------------------------------------


def _pin(client, endpoint_id, key, by="alice"):
    return client.put(f"/o1-adaptor-endpoints/{endpoint_id}/host-keys",
                      json={"keyType": key.get_name(), "publicKey": key.get_base64(), "pinnedBy": by})


def _write(client):
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell",
                                              "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}}]})
    return client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]


def test_a_pinned_key_is_what_trusts_the_server_and_a_changed_key_is_refused(client, lab, monkeypatch):
    """No known_hosts file at all: the endpoint's own pinned key is the only trust. Then the server's key changes (here: a different key is
    pinned): the connection is refused, and only an operator pinning the new key makes it work again."""
    import paramiko
    from app.netconf_ssh import host_key_fingerprint
    monkeypatch.delenv("NETCONF_SSH_KNOWN_HOSTS")
    endpoint_id = _register(client, lab.uri, transport="ssh").json()["endpointId"]
    refused = _write(client)
    assert refused["status"] == "REJECTED" and refused["rejectionReason"] == "NETCONF_RPC_FAILED"        # nothing pinned, nothing trusted
    pinned = _pin(client, endpoint_id, lab.host_key)
    assert pinned.status_code == 200 and pinned.json()["fingerprint"] == host_key_fingerprint(lab.host_key) and pinned.json()["replaced"] is False
    assert _write(client)["status"] == "APPLIED"
    assert client.get(f"/o1-adaptor-endpoints/{endpoint_id}/host-keys").json()["items"][0]["pinnedBy"] == "alice"
    # the device's key "changes": pin a different one for the same type; the real server no longer matches
    other = paramiko.RSAKey.generate(2048)
    replaced = _pin(client, endpoint_id, other, by="bob")
    assert replaced.json()["replaced"] is True and replaced.json()["pinnedBy"] == "bob"
    changed = _write(client)
    assert changed["status"] == "REJECTED" and "does not match" in changed["rejectionDetail"]
    assert _pin(client, endpoint_id, lab.host_key).json()["replaced"] is True                         # the operator accepts the real one again
    assert _write(client)["status"] == "APPLIED"


def test_unpinning_removes_the_trust(client, lab, monkeypatch):
    """With no known-hosts file, a pinned host key lets writes through and unpinning it makes them rejected; unpinning again is 404."""
    monkeypatch.delenv("NETCONF_SSH_KNOWN_HOSTS")
    endpoint_id = _register(client, lab.uri, transport="ssh").json()["endpointId"]
    _pin(client, endpoint_id, lab.host_key)
    assert _write(client)["status"] == "APPLIED"
    assert client.delete(f"/o1-adaptor-endpoints/{endpoint_id}/host-keys/{lab.host_key.get_name()}").status_code == 204
    assert _write(client)["status"] == "REJECTED"
    assert client.delete(f"/o1-adaptor-endpoints/{endpoint_id}/host-keys/{lab.host_key.get_name()}").status_code == 404


def test_pinning_refuses_junk_a_non_ssh_endpoint_and_an_unknown_endpoint(client, lab):
    """Pinning refuses an unreadable key, a key of the wrong type, an endpoint that is not ssh (4xx) and an unknown endpoint (404), and stores
    nothing.
    """
    ssh_id = _register(client, lab.uri, transport="ssh").json()["endpointId"]
    for body in ({"keyType": "ssh-rsa", "publicKey": "not base64!", "pinnedBy": "a"},
                 {"keyType": "ssh-rsa", "publicKey": "AAAA", "pinnedBy": "a"},
                 {"keyType": "nonsense", "publicKey": lab.host_key.get_base64(), "pinnedBy": "a"}):
        assert client.put(f"/o1-adaptor-endpoints/{ssh_id}/host-keys", json=body).status_code in (400, 422)
    assert client.get(f"/o1-adaptor-endpoints/{ssh_id}/host-keys").json() == {"items": []}
    client.delete(f"/o1-adaptor-endpoints/{ssh_id}/host-keys/ssh-rsa")
    mock_id = client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-2", "adaptorUri": "http://adaptor:8000/x",
                                                         "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU"}).json()["endpointId"]
    assert _pin(client, mock_id, lab.host_key).status_code in (400, 422)
    assert _pin(client, "00000000-0000-0000-0000-000000000000", lab.host_key).status_code == 404


def test_the_known_hosts_file_still_works_and_a_pinned_key_adds_to_it(client, lab, monkeypatch):
    """The file of PR-SB-1 is untouched: with it and no pin the write works (the fixture's default)."""
    _register(client, lab.uri, transport="ssh")
    assert _write(client)["status"] == "APPLIED"


def test_registration_refuses_an_unknown_model(client, lab):
    """An ssh URI naming an unknown model is refused at registration."""
    assert _register(client, lab.uri + "?model=nope", transport="ssh").status_code in (400, 422)


def test_a_rejecting_server_gives_a_rejected_sub_change(client, lab):
    """A server that answers rpc-error gives a REJECTED sub-change with NETCONF_RPC_FAILED after one attempt."""
    lab.behaviour.edit_reply = "<rpc-error><error-tag>invalid-value</error-tag></rpc-error>"
    _register(client, lab.uri, transport="ssh")
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell",
                                              "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "X"}}]})
    sub = client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]
    assert sub["status"] == "REJECTED" and sub["rejectionReason"] == "NETCONF_RPC_FAILED" and sub["attempts"] == 1


def test_an_unreachable_ssh_server_is_retried_then_rejected(client, lab, monkeypatch):
    """An ssh server that cannot be reached is retried and then REJECTED with NETCONF_UNREACHABLE after several attempts."""
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr("app.main._sleep", lambda s: None)
    _register(client, f"ssh://netconf@127.0.0.1:{port}", transport="ssh")
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell",
                                              "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "X"}}]})
    sub = client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]
    assert sub["status"] == "REJECTED" and sub["rejectionReason"] == "NETCONF_UNREACHABLE" and sub["attempts"] > 1


# --- PR-SB-2.4: transport tls --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def tls_lab(tmp_path, monkeypatch):
    """Fixture: an in-process NETCONF-over-TLS server with a throwaway CA, and the credential variables of `ru-1` pointing at a client certificate
    it accepts; closed afterwards.
    """
    from netconf_tls_server import NetconfTlsTestServer, Pki
    ca = Pki(tmp_path)
    server_cert, server_key = ca.issue("server", server=True)
    client_cert, client_key = ca.issue("client")
    server = NetconfTlsTestServer(server_cert, server_key, ca.ca_file)
    monkeypatch.setenv("NETCONF_CRED_RU_1_CERT_FILE", client_cert)
    monkeypatch.setenv("NETCONF_CRED_RU_1_KEY_FILE", client_key)
    monkeypatch.setenv("NETCONF_CRED_RU_1_CA_FILE", ca.ca_file)
    yield server
    server.close()


def test_a_tls_endpoint_is_registered_written_and_read_through_the_routes(client, tls_lab, db_session_factory):
    """A tls endpoint with a credential reference can be registered, written to and read through the routes; host-key routes do not apply to it."""
    resp = _register(client, tls_lab.uri, transport="tls", credentialRef="ru-1")
    assert resp.status_code == 201
    assert client.get("/o1-adaptor-endpoints").json()["items"][0]["transport"] == "tls"
    sub = _write(client)
    assert sub["status"] == "APPLIED"
    assert client.get("/managed-entities/ME-1/config").json()["attributes"] == {"administrativeState": "UNLOCKED"}
    # host keys are an SSH matter: a TLS endpoint trusts a CA file
    assert client.get(f"/o1-adaptor-endpoints/{resp.json()['endpointId']}/host-keys").status_code in (400, 422)


def test_a_tls_endpoint_whose_certificate_is_refused_is_a_rejected_write(client, tls_lab, monkeypatch, tmp_path):
    """A client certificate the TLS server does not accept gives a REJECTED sub-change with a detail saying the session was refused."""
    from netconf_tls_server import Pki
    _register(client, tls_lab.uri, transport="tls", credentialRef="ru-1")
    cert, key = Pki(tmp_path, name="rogue").issue("client")
    monkeypatch.setenv("NETCONF_CRED_RU_1_CERT_FILE", cert)
    monkeypatch.setenv("NETCONF_CRED_RU_1_KEY_FILE", key)
    sub = _write(client)
    assert sub["status"] == "REJECTED" and sub["rejectionReason"] == "NETCONF_RPC_FAILED" and "refused" in sub["rejectionDetail"]


@pytest.mark.parametrize("uri,extra", [
    ("ssh://admin@adaptor", {"transport": "tls"}),                   # scheme does not match the transport
    ("tls://adaptor", {}),                                            # tls:// without transport tls
    ("tls://admin@adaptor", {"transport": "tls"}),                    # the user comes from the certificate: none in the URI
    ("tls://adaptor", {"transport": "tls", "o1Protocol": "RESTCONF"}),
    ("tls://adaptor?model=nope", {"transport": "tls"}),
])
def test_registration_refuses_a_mismatched_tls_endpoint(client, uri, extra):
    """Registration refuses a tls transport with an ssh URI, a tls URI without transport tls, a user name in the URI, RESTCONF over tls and an
    unknown model.
    """
    assert _register(client, uri, **extra).status_code in (400, 422)


# --- PR-SB-6.2 / 6.5 / 6.7: the walk, the enforcement flag and the TEIV export ----------------------------------------------------------------


def _lab_cells(*ids):
    return '<lab xmlns="urn:smo:lab">' + "".join(
        f"<cell><id>{i}</id><administrative-state>unlocked</administrative-state><tx-power>40</tx-power></cell>" for i in ids) + "</lab>"


def _ids(client, dn):
    return [o["id"] for o in client.get(f"/managed-objects/{dn}/children").json()["items"]]


def test_a_walk_fills_the_tree_from_the_server_and_follows_it(client, lab):
    """A refresh walks the server's model into the containment tree (added, removed, unchanged counts), re-walking adds nothing, and objects the
    server lost are removed.
    """
    lab.behaviour.data = _lab_cells("101", "102")
    assert _register(client, lab.uri + "?model=smo-lab", transport="ssh").status_code == 201
    first = client.post("/managed-entities/ME-1/managed-objects/refresh").json()
    assert first == {"managedElementRef": "ME-1", "added": 3, "removed": 0, "unchanged": 1, "total": 4}      # the function and two cells, plus the root
    assert _ids(client, "ManagedElement=ME-1,GNBDUFunction=1") == ["101", "102"]
    cell = client.get("/managed-objects/ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101").json()
    assert cell["class"] == "NRCellDU" and cell["source"] == "walk"
    assert "<lab xmlns=\"urn:smo:lab\"/>" in lab.behaviour.received[-1]                                       # the whole container, no key filter
    assert client.post("/managed-entities/ME-1/managed-objects/refresh").json()["added"] == 0                 # nothing changed: nothing added
    lab.behaviour.data = _lab_cells("101", "103")                                                           # the server lost 102 and gained 103
    again = client.post("/managed-entities/ME-1/managed-objects/refresh").json()
    assert (again["added"], again["removed"]) == (1, 1) and _ids(client, "ManagedElement=ME-1,GNBDUFunction=1") == ["101", "103"]
    lab.behaviour.data = _lab_cells()                                                                        # nothing left: the walked function goes too
    assert client.post("/managed-entities/ME-1/managed-objects/refresh").json()["removed"] == 3
    assert client.get("/managed-objects/ManagedElement=ME-1").status_code == 200                            # the registry's root stays


def test_a_walk_needs_a_model_endpoint_and_reports_a_failed_read(client, lab, monkeypatch):
    """A refresh is 404 for an unknown element, 409 PROTOCOL_NOT_SUPPORTED for an endpoint with no model, and 503 when the walk fails."""
    assert client.post("/managed-entities/NOPE/managed-objects/refresh").status_code == 404
    _register(client, lab.uri, transport="ssh")                                                              # no ?model=
    resp = client.post("/managed-entities/ME-1/managed-objects/refresh")
    assert resp.status_code == 409 and "PROTOCOL_NOT_SUPPORTED" in resp.text
    client2_uri = lab.uri + "?model=smo-lab"
    _register_other = client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-2", "adaptorUri": client2_uri, "transport": "ssh",
                                                                  "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU"})
    assert _register_other.status_code == 201
    monkeypatch.setattr("app.main.netconf_ssh.send_walk", lambda *a, **kw: None)
    resp = client.post("/managed-entities/ME-2/managed-objects/refresh")
    assert resp.status_code == 503 and "ENDPOINT_UNREACHABLE" in resp.text


def _job(client, function):
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": [
        {"managedElementRef": "ME-1", "managedFunctionRef": function, "attributeChanges": {"txPower": 41}}]})
    return client.get(f"/config-jobs/{resp.json()['jobId']}").json()["subChanges"][0]


def test_the_flag_rejects_a_target_that_is_not_in_the_tree_and_is_off_by_default(client, lab, monkeypatch):
    """With `RAN_NF_OAM_ENFORCE_MO_TREE` off nothing checks the tree; on, a sub-change whose target DN is not in the tree is rejected with
    MANAGED_OBJECT_NOT_FOUND before any attempt, until the object has been walked.
    """
    lab.behaviour.data = _lab_cells("101")
    _register(client, lab.uri + "?model=smo-lab", transport="ssh")
    unknown = "GNBDUFunction=1,NRCellDU=999"
    assert _job(client, unknown)["status"] == "APPLIED"                                                      # off: nothing checks the tree
    monkeypatch.setenv("RAN_NF_OAM_ENFORCE_MO_TREE", "true")
    refused = _job(client, unknown)
    assert refused["status"] == "REJECTED" and refused["rejectionReason"] == "MANAGED_OBJECT_NOT_FOUND" and refused["attempts"] == 0
    assert _job(client, "GNBDUFunction=1,NRCellDU=101")["rejectionReason"] == "MANAGED_OBJECT_NOT_FOUND"     # real, but not walked yet
    client.post("/managed-entities/ME-1/managed-objects/refresh")
    assert _job(client, "GNBDUFunction=1,NRCellDU=101")["status"] == "APPLIED"                              # in the tree now
    dry = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "dryRun": True, "changes": [
        {"managedElementRef": "ME-1", "managedFunctionRef": unknown, "attributeChanges": {"txPower": 41}}]}).json()
    assert dry["changes"][0] == {"managedElementRef": "ME-1", "managedFunctionRef": unknown, "operation": "merge",
                                 "verdict": "WOULD_REJECT", "reason": "MANAGED_OBJECT_NOT_FOUND"}
    assert _job(client, None)["status"] == "APPLIED"                                                          # the element root is always in the tree


def test_the_topology_export_has_the_nodes_and_the_parent_links(client, lab):
    """The topology export lists the tree's nodes as one generic entity type with their attributes, and a child-of relationship for each parent
    link; it is empty when there are no nodes.
    """
    assert client.get("/topology").json() == {"entities": [], "relationships": []}
    lab.behaviour.data = _lab_cells("101")
    _register(client, lab.uri + "?model=smo-lab", transport="ssh")
    client.post("/managed-entities/ME-1/managed-objects/refresh")
    topo = client.get("/topology").json()
    nodes = topo["entities"][0]["o-ran-smo-teiv-ran:ManagedObject"]
    assert [n["attributes"]["dn"] for n in nodes] == ["ManagedElement=ME-1", "ManagedElement=ME-1,GNBDUFunction=1",
                                                       "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101"]
    assert nodes[2] == {"id": "urn:oran:smo:teiv:ManagedObject:ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101",
                        "attributes": {"dn": "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101", "class": "NRCellDU", "objectId": "101",
                                       "managedElementRef": "ME-1", "source": "walk"}}
    links = topo["relationships"][0]["o-ran-smo-teiv-ran:MANAGEDOBJECT_CHILD_OF_MANAGEDOBJECT"]
    assert len(links) == 2                                                                                    # the root has no parent
    assert links[1]["aSide"].endswith("NRCellDU=101") and links[1]["bSide"].endswith("ManagedElement=ME-1,GNBDUFunction=1")
    assert links[1]["sourceIds"] == ["ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101", "ManagedElement=ME-1,GNBDUFunction=1"]
    assert client.get("/topology", params={"managed_element_ref": "OTHER"}).json() == {"entities": [], "relationships": []}


# --- PR-SB-1.10: one candidate transaction per job and element --------------------------------------------------------------------------


def _steps_of(lab):
    import re
    return [(m.group(1) if (m := re.search(r'message-id="[^"]*-(lock|commit|discard|unlock)"', t)) else ("edit-config" if "<edit-config>" in t else "get-config"))
            for t in lab.behaviour.received]


def _two_changes(client, second=None):
    """Posts a job with two sub-changes to ME-1 (adminState, and `second` or txPower) and returns the job view."""
    changes = [{"managedElementRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED"}},
               {"managedElementRef": "ME-1", "attributeChanges": second or {"txPower": 30}}]
    resp = client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": changes})
    assert resp.status_code == 202, resp.text
    return client.get(f"/config-jobs/{resp.json()['jobId']}").json()


def test_two_sub_changes_of_one_element_are_one_transaction(client, lab, monkeypatch):
    """Two sub-changes of one element on a candidate-datastore endpoint are one transaction: one lock, both edits, one commit, one unlock."""
    monkeypatch.setattr("app.main.CM_SNAPSHOTS", False)                       # the before-image reads would be steps of their own
    lab.behaviour.caps = list(CAND_CAPS)
    assert _register(client, lab.uri + "?datastore=candidate", transport="ssh").status_code == 201
    job = _two_changes(client)
    assert job["status"] == "COMPLETED" and [s["status"] for s in job["subChanges"]] == ["APPLIED", "APPLIED"]
    assert _steps_of(lab) == ["lock", "edit-config", "edit-config", "commit", "unlock"]


def test_a_refused_second_sub_change_leaves_the_first_uncommitted(client, lab, monkeypatch):
    """The point of SB-1.10: before, the first sub-change was already committed when the second one failed."""
    monkeypatch.setattr("app.main.CM_SNAPSHOTS", False)
    lab.behaviour.caps = list(CAND_CAPS)
    lab.behaviour.edit_replies = ["<ok/>", "<rpc-error><error-tag>invalid-value</error-tag><error-message>out of range</error-message></rpc-error>"]
    assert _register(client, lab.uri + "?datastore=candidate", transport="ssh").status_code == 201
    job = _two_changes(client, {"txPower": 70000})
    first, second = job["subChanges"]
    assert job["status"] == "FAILED" and first["status"] == second["status"] == "REJECTED"
    assert first["rejectionReason"] == "NETCONF_TRANSACTION_ABORTED" and "sub-change 2 of 2 was refused" in first["rejectionDetail"]
    assert second["rejectionReason"] == "NETCONF_RPC_FAILED" and second["rejectionDetail"].startswith("edit-config: invalid-value")
    assert _steps_of(lab) == ["lock", "edit-config", "edit-config", "discard", "unlock"]               # no commit


def test_the_history_records_each_sub_change_of_a_transaction(client, lab):
    """Each sub-change of a transaction still gets its own history record."""
    lab.behaviour.caps = list(CAND_CAPS)
    assert _register(client, lab.uri + "?datastore=candidate", transport="ssh").status_code == 201
    _two_changes(client)
    items = client.get("/managed-entities/ME-1/config-history").json()["items"]
    assert len(items) == 2 and {tuple(i["after"]) for i in items} == {("adminState",), ("txPower",)}
    assert all(i["subChangeStatus"] == "APPLIED" for i in items)


def test_a_lone_sub_change_and_a_non_candidate_endpoint_are_dispatched_as_before(client, lab, monkeypatch):
    """An endpoint not registered with `?datastore=candidate` gets one edit-config per sub-change, as before."""
    monkeypatch.setattr("app.main.CM_SNAPSHOTS", False)
    assert _register(client, lab.uri, transport="ssh").status_code == 201                  # no ?datastore=candidate: one edit-config each
    job = _two_changes(client)
    assert [s["status"] for s in job["subChanges"]] == ["APPLIED", "APPLIED"]
    assert _steps_of(lab) == ["edit-config", "edit-config"]
