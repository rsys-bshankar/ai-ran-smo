"""PR-SB-9b (SB-9.3 to 9.5, 9.8): the O1 conformance kit's emitting groups (FM, PM, SW, HB) against `mock-o1-adaptor` wired to the real RAN NF OAM app.

The mesh (mesh.py) lands the stub's `httpx.post` to `http://ran-nf-oam:8000` on RAN NF OAM's app, which has its own DME behind R1 for the PM subscription; the
kit reads RAN NF OAM back through the same app. Fakes, each breaking one thing, show the checks are read-backs and not 2xx counts.
"""

import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conformance.o1 import checks  # noqa: E402,F401
from conformance.o1.__main__ import main  # noqa: E402
from conformance.o1.kit import FAIL, PASS, REGISTRY, SKIP, Context, run, summary  # noqa: E402

EMIT_GROUPS = {"FM", "PM", "SW", "HB"}


@pytest.fixture(autouse=True)
def stub_targets_oam(monkeypatch):
    monkeypatch.setenv("MOCK_O1_OAM_URL", "http://ran-nf-oam:8000")
    monkeypatch.delenv("MOCK_O1_SUPPORTED_SERVICES", raising=False)


@pytest.fixture(autouse=True)
def element_rpcs_reach_the_stub(mesh):
    """The kit registers its element at the adaptor client's own base URL (`testserver`, or `fake-adaptor` for the broken adaptors below), and RAN NF OAM
    sends a software job's RPCs there (call flow 19b): the mesh routes both hosts to the stub, as the network does in a real run."""
    aliases = [host for host in ("testserver", "fake-adaptor") if host not in mesh]
    for host in aliases:
        mesh[host] = mesh["mock-o1-adaptor"]
    yield
    for host in aliases:
        mesh.pop(host, None)


def ctx_for(adaptor, oam, protocols=()) -> Context:
    return Context(adaptor, set(protocols), oam=oam)


def outcome(results) -> dict[str, str]:
    return {r.id: r.status for r in results}


def failures(results) -> dict[str, str]:
    return {r.id: r.detail for r in results if r.status == FAIL}


def relay(mock, request: httpx.Request, content: bytes | None = None) -> httpx.Response:
    """Forwards an httpx request to a test client (the mock adaptor in the mesh), with an optional replacement body, and returns the answer as an
    httpx response.
    """
    resp = mock.request(request.method, request.url.raw_path.decode(), content=request.content if content is None else content,
                        headers={"content-type": request.headers.get("content-type", "")})
    return httpx.Response(resp.status_code, content=resp.content, headers={"content-type": resp.headers.get("content-type", "application/json")})


def forwarding(mock, handler=None) -> httpx.Client:
    """An adaptor in front of the stub: `handler(request)` may answer a request itself (a fake); returning None passes it on."""
    def pass_on(request: httpx.Request) -> httpx.Response:
        answer = handler(request) if handler else None
        return answer if answer is not None else relay(mock, request)
    return httpx.Client(base_url="http://fake-adaptor", transport=httpx.MockTransport(pass_on))


def test_the_stub_wired_to_ran_nf_oam_passes_every_emitting_check(mesh):
    """With the mock adaptor wired to the real RAN NF OAM, every check of the emitting groups (FM, PM, SW, HB) passes."""
    results = run(ctx_for(mesh["mock-o1-adaptor"], mesh["ran-nf-oam"]), EMIT_GROUPS)
    assert failures(results) == {}
    assert {r.id for r in results} >= {"FM-1", "FM-4", "PM-1", "PM-3", "SW-2", "HB-2"}
    assert summary(results) == {PASS: len(results), FAIL: 0, SKIP: 0}


def test_the_whole_kit_passes_with_both_protocols_and_a_second_run_passes_too(mesh):
    """The whole kit passes with NETCONF and RESTCONF, and passes again on a second run against the same state."""
    stub, oam = mesh["mock-o1-adaptor"], mesh["ran-nf-oam"]
    for _ in range(2):
        results = run(ctx_for(stub, oam, ("netconf", "restconf")))
        assert failures(results) == {}
        assert summary(results) == {PASS: len(REGISTRY), FAIL: 0, SKIP: 0}


def test_what_the_kit_registers_is_what_the_adaptor_declares(mesh, monkeypatch):
    """The kit registers the element with the services the adaptor declares, runs the groups it declares and skips the others with a reason that
    names the missing service.
    """
    monkeypatch.setenv("MOCK_O1_SUPPORTED_SERVICES", "PROV,FM,HEARTBEAT")
    stub, oam = mesh["mock-o1-adaptor"], mesh["ran-nf-oam"]
    ctx = ctx_for(stub, oam)
    results = run(ctx, EMIT_GROUPS)
    assert failures(results) == {}
    status = outcome(results)
    assert {i for i, s in status.items() if s == PASS} == {"FM-1", "FM-2", "FM-3", "FM-4", "HB-1", "HB-2"}
    assert {i for i, s in status.items() if s == SKIP} == {"PM-1", "PM-2", "PM-3", "SW-1", "SW-2", "SW-3"}
    skipped = {r.id: r.detail for r in results if r.status == SKIP}
    assert "does not declare PM" in skipped["PM-1"] and "does not declare SWM" in skipped["SW-1"] and "does not declare FILE" in skipped["PM-2"]
    ref = ctx.prepared["element"][0]
    endpoint = next(e for e in oam.get("/o1-adaptor-endpoints").json()["items"] if e["managedElementRef"] == ref)
    assert endpoint["supportedServices"] == ["FM", "HEARTBEAT", "PROV"]


def test_without_an_oam_url_the_emitting_groups_are_skipped_and_say_why(mesh):
    """Without an OAM URL all emitting checks are skipped and each says `--oam-url` is needed."""
    results = run(ctx_for(mesh["mock-o1-adaptor"], None), EMIT_GROUPS)
    assert {r.status for r in results} == {SKIP} and all("--oam-url" in r.detail for r in results)


def test_an_existing_element_can_be_named(mesh):
    """The kit can run against an element that is already registered, leaving alarms there, and fails with a clear message for an element that has
    no adaptor endpoint.
    """
    stub, oam = mesh["mock-o1-adaptor"], mesh["ran-nf-oam"]
    oam.post("/o1-adaptor-endpoints", json={"managedElementRef": "gnb-given-01", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
                                            "o1Protocol": "NETCONF", "entityType": "O-DU"}).raise_for_status()
    ctx = Context(stub, set(), oam=oam, element="gnb-given-01")
    assert failures(run(ctx, EMIT_GROUPS)) == {}
    assert oam.get("/alarms", params={"managed_element_ref": "gnb-given-01"}).json()["total"] >= 4
    missing = Context(stub, set(), oam=oam, element="gnb-nobody")
    assert "no O1 adaptor endpoint" in failures(run(missing, {"FM-1"}))["FM-1"]


def test_the_runner_takes_the_urls_and_reports_the_oam_it_read(mesh, tmp_path, capsys):
    """The command-line runner takes the adaptor, OAM and emit URLs, runs the selected groups, prints the totals and records the OAM URL it read in
    the JSON report.
    """
    out = tmp_path / "report"
    code = main(["--adaptor", "http://mock", "--oam-url", "http://oam", "--emit-url", "http://mock", "--only", "FM", "--only", "HB", "--out", str(out)],
                client=mesh["mock-o1-adaptor"], oam_client=mesh["ran-nf-oam"], emit_client=mesh["mock-o1-adaptor"])
    assert code == 0
    text = capsys.readouterr().out
    assert "FM-1" in text and "HB-2" in text and "http://oam" in text and "6 passed, 0 failed, 0 skipped" in text
    assert '"oam": "http://oam"' in (tmp_path / "report.json").read_text()


# ---- adaptors that break one thing

def says_emitted_and_sends_nothing(request: httpx.Request):
    """A fake adaptor trigger that answers `emitted` for every `/emit/` call but sends nothing to RAN NF OAM."""
    if request.url.path.startswith("/emit/"):
        return httpx.Response(200, json={"emitted": True, "status": 201, "response": {"alarmId": str(uuid.uuid4()), "fileId": str(uuid.uuid4())}, "target": "x"})
    return None


def test_an_adaptor_that_says_emitted_and_sends_nothing_fails_every_read_back(mesh):
    """An adaptor that says it emitted but sent nothing fails every check that reads RAN NF OAM back, so the checks are read-backs and not 2xx
    counts.
    """
    bad = failures(run(ctx_for(forwarding(mesh["mock-o1-adaptor"], says_emitted_and_sends_nothing), mesh["ran-nf-oam"]), EMIT_GROUPS))
    assert {"FM-2", "FM-3", "FM-4", "PM-2", "PM-3", "SW-1", "SW-2", "SW-3", "HB-1", "HB-2"} <= set(bad)
    assert "lists 0 alarms" in bad["FM-2"] and "lastHeartbeatAt" in bad["HB-1"] and "DOWNLOAD phase" in bad["SW-1"]


def test_an_adaptor_that_sends_the_wrong_severity_fails_the_severity_check_only(mesh):
    """An adaptor that sends a different alarm severity fails only the severity check, with both values in the message."""
    stub = mesh["mock-o1-adaptor"]

    def downgrade(request: httpx.Request):
        if request.url.path == "/emit/alarm":
            return relay(stub, request, json.dumps({**json.loads(request.content), "severity": "WARNING"}).encode())
        return None
    bad = failures(run(ctx_for(forwarding(stub, downgrade), mesh["ran-nf-oam"]), {"FM"}))
    assert set(bad) == {"FM-3"} and "'Critical'" in bad["FM-3"] and "'warning'" in bad["FM-3"]


def test_an_adaptor_that_never_reports_the_software_phase_fails_the_software_checks(mesh):
    """An adaptor that never reports the software phase fails the three software checks and no others."""
    def swallow(request: httpx.Request):
        if request.url.path == "/emit/software-phase":
            return httpx.Response(200, json={"emitted": True, "status": 200, "response": {}, "target": "x"})
        return None
    results = run(ctx_for(forwarding(mesh["mock-o1-adaptor"], swallow), mesh["ran-nf-oam"]), EMIT_GROUPS)
    bad = failures(results)
    assert set(bad) == {"SW-1", "SW-2", "SW-3"}
    assert "reads phase DOWNLOAD" in bad["SW-1"] and "DOWNLOAD/IN_PROGRESS" in bad["SW-2"]


def test_an_adaptor_without_the_trigger_api_fails_with_what_it_answered(mesh):
    """An adaptor without the `/emit/` trigger API fails every emitting check and says what it answered."""
    def no_triggers(request: httpx.Request):
        return httpx.Response(404, text="not here") if request.url.path.startswith("/emit/") else None
    bad = failures(run(ctx_for(forwarding(mesh["mock-o1-adaptor"], no_triggers), mesh["ran-nf-oam"]), EMIT_GROUPS))
    assert len(bad) == len([c for c in REGISTRY if c.group in EMIT_GROUPS]) and "/emit/alarm answered 404" in bad["FM-1"]


def test_a_refusal_from_ran_nf_oam_is_a_failure_that_names_it(mesh):
    # an element whose endpoint does not offer FM: RAN NF OAM refuses the alarm, the stub relays that (emitted false), the check says what RAN NF OAM answered
    """When RAN NF OAM refuses an alarm for an element whose endpoint does not offer FM, the check fails and names both that the adaptor did not
    emit and what RAN NF OAM answered.
    """
    stub, oam = mesh["mock-o1-adaptor"], mesh["ran-nf-oam"]
    made = oam.post("/o1-adaptor-endpoints", json={"managedElementRef": "gnb-no-fm", "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
                                                   "o1Protocol": "NETCONF", "entityType": "O-DU", "supportedServices": ["PROV"]})
    made.raise_for_status()
    ctx = ctx_for(stub, oam)
    ctx.prepared["element"] = ("gnb-no-fm", made.json()["endpointId"])
    bad = failures(run(ctx, {"FM-1"}))
    assert "did not emit alarm" in bad["FM-1"] and "does not support MnS service FM" in bad["FM-1"]
