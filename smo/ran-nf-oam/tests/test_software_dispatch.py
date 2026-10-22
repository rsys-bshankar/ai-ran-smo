"""Tests of call flow 19 (DOC-CF-1.8): a software management job is sent to its element. Starting a job sends `software-download` with the package's remote
path to the element's O1 adaptor; each reported phase sends the next RPC; an RPC the element refuses, or an element that cannot be reached, fails the job with
the reason. One job at a time per element, an unknown element is a 404, a job can be read and aborted, reporting a phase needs MSAC `exec` like starting
one, and a completed job records its version on the element and is announced on the event feed.

These tests are marked `real_software_rpc`, so the conftest's stand-in adaptor is off and the NETCONF client's HTTP post is patched instead. Fixtures come from
`test_main.py`. Run: `cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/test_software_dispatch.py -q`.
"""

import httpx
import pytest

from smo_shared import events

from app.models import ElementOnboarding
from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures and builder)

pytestmark = pytest.mark.real_software_rpc
OK = '<rpc-reply message-id="m" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
REFUSED = ('<rpc-reply message-id="m" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><rpc-error><error-type>application</error-type>'
           '<error-tag>operation-failed</error-tag><error-message>no space left</error-message></rpc-error></rpc-reply>')


@pytest.fixture
def adaptor(monkeypatch):
    """The element's adaptor: records every RPC posted to it and answers with `reply` (a reply body, or None for no answer)."""
    class Adaptor(list):
        reply: str | None = OK

    sent = Adaptor()

    def post(url, content=None, headers=None, timeout=None, **kw):
        sent.append(content)
        if sent.reply is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=sent.reply)

    monkeypatch.setattr("app.netconf_client.httpx.post", post)
    return sent


def _start(client, **params):
    """Starts a software job on ME-1 for the version 2.0 package; returns the response."""
    return client.post("/software-management-jobs", params={"managed_element_ref": "ME-1", "remote_file_path": "sftp://repo/du-2.0.zip",
                                                            "software_version": "2.0", **params})


def test_each_phase_is_sent_to_the_element_and_the_completed_version_is_recorded(client, db_session_factory, adaptor, monkeypatch):
    """Call flow 19b: the start sends software-download with the remote path; each successful report sends the next RPC (install of the file into the
    standby slot, then activate); the completed job records 2.0 on the element and every change is published as software-job.state-changed."""
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    events.MEMORY.events.clear()
    _make_me(db_session_factory)
    with db_session_factory() as db:
        db.add(ElementOnboarding(managed_element_ref="ME-1", status="READY", software_version="1.0"))
        db.commit()
    job = _start(client).json()
    assert (job["status"], job["phase"], job["dispatchedPhase"]) == ("IN_PROGRESS", "DOWNLOAD", "DOWNLOAD")
    assert "<software-download" in adaptor[0] and "<remote-file-path>sftp://repo/du-2.0.zip</remote-file-path>" in adaptor[0]
    client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True})
    assert "<software-install" in adaptor[1] and "<file-names>du-2.0.zip</file-names>" in adaptor[1] and "<slot-name>standby</slot-name>" in adaptor[1]
    client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True})
    assert "<software-activate" in adaptor[2]
    done = client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True}).json()
    assert done["status"] == "COMPLETED" and len(adaptor) == 3
    with db_session_factory() as db:
        assert db.get(ElementOnboarding, "ME-1").software_version == "2.0"
    assert [e["data"]["to"] for e in events.MEMORY.events if e["type"] == "org.oran.smo.software-job.state-changed"] == ["IN_PROGRESS", "COMPLETED"]


def test_an_rpc_the_element_refuses_or_cannot_get_fails_the_job_with_the_reason(client, db_session_factory, adaptor):
    """Call flow 19b: an rpc-error on the download fails the job at once, the error kept as `failureReason`; an adaptor that does not answer the install does
    too; a job on an element with no endpoint fails ENDPOINT_UNREACHABLE."""
    _make_me(db_session_factory)
    adaptor.reply = REFUSED
    refused = _start(client).json()
    assert refused["status"] == "FAILED" and refused["phase"] == "DOWNLOAD" and "operation-failed" in refused["failureReason"] and "no space left" in refused["failureReason"]
    adaptor.reply = OK
    job = _start(client).json()
    adaptor.reply = None
    failed = client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": True}).json()
    assert (failed["status"], failed["phase"]) == ("FAILED", "INSTALL") and "NETCONF_UNREACHABLE" in failed["failureReason"]


def test_one_job_at_a_time_unknown_element_404_read_and_abort(client, db_session_factory, adaptor):
    """Call flow 19a/19c: a second job on a busy element is 409 SOFTWARE_JOB_IN_PROGRESS; an unknown element is 404; a missing remote path is 422; a job can be
    read, and aborted (FAILED, "aborted", nothing sent), after which another may start; an ended job cannot be aborted again (409)."""
    _make_me(db_session_factory)
    job = _start(client).json()
    busy = _start(client)
    assert busy.status_code == 409 and busy.json()["detail"]["title"] == "SOFTWARE_JOB_IN_PROGRESS"
    assert _start(client, managed_element_ref="ME-404").status_code == 404
    assert client.post("/software-management-jobs", params={"managed_element_ref": "ME-1"}).status_code == 422
    assert client.get(f"/software-management-jobs/{job['jobId']}").json()["remoteFilePath"] == "sftp://repo/du-2.0.zip"
    aborted = client.post(f"/software-management-jobs/{job['jobId']}/abort", params={"reason": "element replaced"}).json()
    assert aborted["status"] == "FAILED" and aborted["failureReason"] == "aborted: element replaced" and len(adaptor) == 1
    assert client.post(f"/software-management-jobs/{job['jobId']}/abort").status_code == 409
    assert _start(client).status_code == 202


def test_a_report_with_a_reason_keeps_it(client, db_session_factory, adaptor):
    """Call flow 19c: `succeeded=false` with a `reason` fails the job with that reason; without one the reason says the element reported the failure."""
    _make_me(db_session_factory)
    job = _start(client).json()
    failed = client.post(f"/software-management-jobs/{job['jobId']}/advance", params={"succeeded": False, "reason": "checksum mismatch"}).json()
    assert failed["failureReason"] == "checksum mismatch"


def test_a_send_that_breaks_fails_the_job_not_the_request(client, db_session_factory, monkeypatch):
    """Call flow 19b: an error raised while sending (not a transport error the client answers) still answers 202, with the job FAILED and the error's
    type in the reason, so a claimed phase never leaves a job waiting for a report that cannot come."""
    def broken(*a, **kw):
        raise LookupError("no route")

    monkeypatch.setattr("app.netconf_client.httpx.post", broken)
    _make_me(db_session_factory)
    started = _start(client)
    assert started.status_code == 202
    assert started.json()["status"] == "FAILED" and "LookupError" in started.json()["failureReason"]
