"""The emitting checks of the O1 adaptor kit (PR-SB-9b): groups FM, PM (and FILE), SW and HB.

These run the other way from checks.py: the adaptor sends alarms, PM reports and files, software-management phase results and heartbeats to RAN NF OAM. There is no
standard way to ask an adaptor to do that, so the kit uses a trigger API (`POST <emit-url>/emit/<kind>`, the one `mock-o1-adaptor` implements), and every check then reads
RAN NF OAM's own state: a trigger that answers "emitted" and sends nothing fails the read-back. A group is skipped when the adaptor does not declare its service in
`/capabilities`, or when no `--oam-url` was given (kit.run).

The kit prepares what RAN NF OAM needs once per run and caches it in `ctx.prepared`: a managed element with an O1 adaptor endpoint (or the `--element` given), and a PM
subscription for a counter new to the run. It never removes them. Ids are listed in `conformance/README.md`, and the README's trigger table is the contract a vendor's test
hook must meet. PM-1 reads RAN NF OAM's answer to the report because a report is not stored there (it goes to DME); the other checks read stored state.
"""

import datetime
import re
import uuid

import httpx

from .checks import KNOWN_SERVICES
from .kit import Context, Fail, check

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _excerpt(resp: httpx.Response) -> str:
    """The first 200 characters of a response body on one line, for a failure message."""
    return resp.text[:200].replace("\n", " ")


def _oam_client(ctx: Context) -> httpx.Client:
    """RAN NF OAM's client, or `Fail` when the run has none (kit.run skips the group before this, so this only guards a check called by hand)."""
    if ctx.oam is None:     # kit.run skips the group before this; a check called by hand without one says so
        raise Fail("no --oam-url: there is no RAN NF OAM to read back from")
    return ctx.oam


def _oam(ctx: Context, method: str, path: str, what: str, *, params: dict | None = None, json: dict | None = None, ok: tuple[int, ...] = (200, 201, 202)):
    """A call to RAN NF OAM that must answer one of `ok`; its JSON body. A refusal or a non-JSON answer is a `Fail` that names `what` could not be prepared or read."""
    oam = _oam_client(ctx)
    resp = oam.request(method, path, params=params, json=json)
    if resp.status_code not in ok:
        raise Fail(f"{what}: RAN NF OAM answered {resp.status_code} to {method} {path}: {_excerpt(resp)}")
    try:
        return resp.json()
    except ValueError:
        raise Fail(f"{what}: RAN NF OAM's answer to {method} {path} is not JSON") from None


# Only the first 500 items are read (no paging): on a stack with more entries for the filter than that, the item a check looks for may not be in the list and the check fails.
def _items(ctx: Context, path: str, what: str, params: dict) -> list[dict]:
    """The `items` list of a RAN NF OAM list endpoint, asking for up to 500; fails when the answer has no `items` list."""
    body = _oam(ctx, "GET", path, what, params={**params, "limit": 500})
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise Fail(f"{what}: GET {path} has no items list")
    return items


def _trigger(ctx: Context, kind: str, body: dict) -> dict:
    """Ask the adaptor to emit one message of `kind` and return its answer `{emitted, status, response}`.

    `emitted` is the adaptor's word only; a trigger that answers `emitted: false` fails here, naming what RAN NF OAM answered, but one that answers true proves nothing,
    so every caller reads RAN NF OAM's state afterwards.
    """
    resp = ctx.emit.post(f"{ctx.emit_prefix}/{kind}", json=body)
    if resp.status_code >= 300:
        raise Fail(f"the trigger POST {ctx.emit_prefix}/{kind} answered {resp.status_code}: {_excerpt(resp)}")
    try:
        answer = resp.json()
    except ValueError:
        raise Fail(f"the trigger POST {ctx.emit_prefix}/{kind} did not answer JSON") from None
    if not isinstance(answer, dict) or "emitted" not in answer:
        raise Fail(f"the trigger POST {ctx.emit_prefix}/{kind} did not answer {{emitted, status, response}}: {_excerpt(resp)}")
    if answer["emitted"] is not True:
        raise Fail(f"the adaptor says it did not emit {kind}: RAN NF OAM answered {answer.get('status')}: {str(answer.get('response'))[:200]}")
    return answer


def _services(ctx: Context) -> list[str]:
    """The services to register the run's element with: the adaptor's declared services that RAN NF OAM knows, or all known ones when it has declared none."""
    declared = ctx.declared.get("supportedServices")
    return sorted(s for s in declared if s in KNOWN_SERVICES) if isinstance(declared, list) else sorted(KNOWN_SERVICES)


def _endpoint_of(ctx: Context, ref: str) -> dict:
    """The O1 adaptor endpoint RAN NF OAM holds for the managed element `ref`, or `Fail` when there is none."""
    for ep in _items(ctx, "/o1-adaptor-endpoints", "reading the registered endpoints", {}):
        if ep.get("managedElementRef") == ref:
            return ep
    raise Fail(f"RAN NF OAM has no O1 adaptor endpoint for the managed element {ref!r}")


def _element(ctx: Context) -> tuple[str, str]:
    """`(managed element reference, endpoint id)` the emitting checks use, resolved once per run and cached in `ctx.prepared`.

    With `--element` it is that element's registered endpoint. Otherwise the kit registers a new element at RAN NF OAM (`POST /o1-adaptor-endpoints`) whose adaptor URI is the
    adaptor's NETCONF URL and whose supported services are the ones the adaptor declares, so that RAN NF OAM's `require_service` check has an answer for them.
    """
    if "element" not in ctx.prepared:
        if ctx.element:
            ctx.prepared["element"] = (ctx.element, _endpoint_of(ctx, ctx.element)["endpointId"])
        else:
            ref = ctx.new_ref("conf-me")
            uri = str(ctx.client.base_url).rstrip("/") + ctx.netconf_path
            made = _oam(ctx, "POST", "/o1-adaptor-endpoints", "registering a managed element for the run",
                        json={"managedElementRef": ref, "adaptorUri": uri, "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF",
                              "entityType": "O-DU", "supportedServices": _services(ctx)})
            ctx.prepared["element"] = (ref, made["endpointId"])
    return ctx.prepared["element"]


def _subscribed_counter(ctx: Context) -> str:
    """The counter type of the PM subscription this run made on its element, creating it on first use.

    The subscription is read back from the subscription list before it is used, so a 2xx on creation is not taken on trust. The counter name carries the run id so a second
    run does not meet the first one's subscription. Without the subscription RAN NF OAM refuses reports and files for the counter.
    """
    if "counter" not in ctx.prepared:
        ref, _ = _element(ctx)
        counter = f"ConfPm{ctx.run_id}"
        made = _oam(ctx, "POST", "/pm-subscriptions", "subscribing to PM on the managed element", ok=(200, 201),
                    params={"managed_element_ref": ref, "counter_type": counter, "delivery_method": "pull"})
        listed = _items(ctx, "/pm-subscriptions", "reading the PM subscriptions", {"managed_element_ref": ref})
        if not any(s.get("subscriptionId") == made.get("subscriptionId") and s.get("counterType") == counter for s in listed):
            raise Fail(f"the PM subscription {made.get('subscriptionId')} on {ref} is not in the subscription list")
        ctx.prepared["counter"] = counter
    return ctx.prepared["counter"]


def _source_id(ctx: Context) -> str:
    """A source alarm id new to this run and to this call, so a check can pick its own alarm out of the list."""
    return f"conf-{ctx.run_id}-{uuid.uuid4().hex[:6]}"


def _body_of(answer: dict) -> dict:
    """The adaptor's relayed body of RAN NF OAM's answer (`response`) when it is a JSON object, else an empty dict."""
    return answer["response"] if isinstance(answer.get("response"), dict) else {}


# ------------------------------------------------------------------------------------------------------------ FM

def _alarms(ctx: Context, ref: str, source_id: str) -> list[dict]:
    """The alarms RAN NF OAM lists for the element whose `sourceAlarmId` is `source_id`."""
    return [a for a in _items(ctx, "/alarms", "reading the alarms", {"managed_element_ref": ref}) if a.get("sourceAlarmId") == source_id]


@check("FM-1", "FM", "an alarm the adaptor raises is accepted by RAN NF OAM's ingest, which answers 200 with an alarmId", "FM")
def fm_accepted(ctx: Context) -> None:
    """FM-1: RAN NF OAM's alarm ingest answers 200 with a string `alarmId` to an alarm the adaptor raises."""
    ref, _ = _element(ctx)
    answer = _trigger(ctx, "alarm", {"managedElementRef": ref, "severity": "MAJOR"})
    if answer.get("status") != 200 or not isinstance(_body_of(answer).get("alarmId"), str):
        raise Fail(f"RAN NF OAM's answer to the ingest was {answer.get('status')} {answer.get('response')}, not 200 with an alarmId")


@check("FM-2", "FM", "RAN NF OAM lists the alarm with the fields the adaptor sent (cause, problem, type, correlation group)", "FM")
def fm_listed_with_fields(ctx: Context) -> None:
    """FM-2: exactly one listed alarm matches the source id, and it carries the cause, problem, type and correlation group the adaptor sent."""
    ref, _ = _element(ctx)
    source = _source_id(ctx)
    sent = {"managedElementRef": ref, "severity": "MINOR", "sourceAlarmId": source, "probableCause": "linkFailure",
            "specificProblem": "conformance probe", "alarmType": "COMMUNICATIONS_ALARM", "correlationGroup": f"grp-{ctx.run_id}"}
    _trigger(ctx, "alarm", sent)
    found = _alarms(ctx, ref, source)
    if len(found) != 1:
        raise Fail(f"RAN NF OAM lists {len(found)} alarms for source id {source} on {ref} after the adaptor said it raised one")
    wrong = [f"{k} {found[0].get(k)!r} (sent {v!r})" for k, v in sent.items() if k != "severity" and found[0].get(k) != v]
    if wrong:
        raise Fail("the listed alarm differs from the one sent: " + ", ".join(wrong))


@check("FM-3", "FM", "the severity is taken as a PerceivedSeverity whatever its case: listed in lower case, with the upper-case perceivedSeverity", "FM")
def fm_severity(ctx: Context) -> None:
    """FM-3: a severity is accepted in any case and stored lower case, with the upper-case `perceivedSeverity` (a PerceivedSeverity, whatever its case)."""
    ref, _ = _element(ctx)
    for sent, stored in (("Critical", "critical"), ("warning", "warning")):
        source = _source_id(ctx)
        _trigger(ctx, "alarm", {"managedElementRef": ref, "severity": sent, "sourceAlarmId": source})
        found = _alarms(ctx, ref, source)
        if len(found) != 1:
            raise Fail(f"after a {sent!r} alarm RAN NF OAM lists {len(found)} alarms for source id {source}")
        if found[0].get("severity") != stored or found[0].get("perceivedSeverity") != stored.upper():
            raise Fail(f"an alarm sent with severity {sent!r} is listed as severity {found[0].get('severity')!r} / perceivedSeverity {found[0].get('perceivedSeverity')!r}")


@check("FM-4", "FM", "the alarmId is minted by RAN NF OAM: a UUID that is not the adaptor's own id, new for each alarm even when the adaptor repeats its id", "FM")
def fm_fresh_alarm_id(ctx: Context) -> None:
    """FM-4: RAN NF OAM mints the `alarmId`: two alarms with the same adaptor id get two different UUIDs, neither equal to the adaptor's id."""
    ref, _ = _element(ctx)
    source = _source_id(ctx)
    for _ in range(2):
        _trigger(ctx, "alarm", {"managedElementRef": ref, "severity": "MAJOR", "sourceAlarmId": source})
    ids = [a.get("alarmId") for a in _alarms(ctx, ref, source)]
    if len(ids) != 2 or len(set(ids)) != 2:
        raise Fail(f"two alarms with the same source id {source} are listed as {len(ids)} alarm(s) with alarmIds {ids}")
    if any(not isinstance(i, str) or not UUID_RE.match(i) or i == source for i in ids):
        raise Fail(f"the alarmIds {ids} are not new UUIDs")


# ------------------------------------------------------------------------------------------------------------ PM and files

@check("PM-1", "PM", "after a PM subscription on the element, a report of its counter is accepted (201) and its measurements are counted", "PM")
def pm_report_accepted(ctx: Context) -> None:
    """PM-1: a report for the subscribed counter is answered 201 with the counter, the element and the number of measurements (2) it counted."""
    ref, _ = _element(ctx)
    counter = _subscribed_counter(ctx)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    answer = _trigger(ctx, "pm-report", {"managedElementRef": ref, "counterType": counter, "measurements": [
        {"cellId": "101", "timestamp": now, "value": 41.5}, {"cellId": "102", "timestamp": now, "values": {"RRU.PrbTotDl": 12.0, "RRU.PrbTotUl": 7.5}}]})
    got = _body_of(answer)
    if answer.get("status") != 201 or got.get("measurements") != 2 or got.get("counterType") != counter or got.get("managedElementRef") != ref:
        raise Fail(f"RAN NF OAM's answer to the report was {answer.get('status')} {answer.get('response')}, not 201 counting 2 measurements of {counter} on {ref}")


def _file(ctx: Context, cells: list[str]) -> tuple[dict, str]:
    """Have the adaptor report a performance file with one measurement per cell in `cells`; returns RAN NF OAM's answer (with the `fileId`) and the counter used.

    Fails unless RAN NF OAM answered 201 with a string `fileId`.
    """
    ref, _ = _element(ctx)
    counter = _subscribed_counter(ctx)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    answer = _trigger(ctx, "pm-file", {"managedElementRef": ref, "counterType": counter, "fileDataType": "Performance", "fileFormat": "json",
                                       "measurements": [{"cellId": c, "timestamp": now, "value": 50.0 + i} for i, c in enumerate(cells)]})
    got = _body_of(answer)
    if answer.get("status") != 201 or not isinstance(got.get("fileId"), str):
        raise Fail(f"RAN NF OAM's answer to the file was {answer.get('status')} {answer.get('response')}, not 201 with a fileId")
    return got, counter


@check("PM-2", "PM", "a performance file the adaptor reports is accepted (201) and listed by GET /files with its format and a size", "FILE")
def pm_file_listed(ctx: Context) -> None:
    """PM-2: the reported file is listed once by `GET /files` (matched by the file id in its location) with format `json` and a size above zero."""
    got, _ = _file(ctx, ["201", "202"])
    listed = _items(ctx, "/files", "reading the files", {"fileDataType": "Performance", "beginTime": ctx.started.isoformat()})
    mine = [f for f in listed if str(f.get("fileLocation", "")).endswith(f"/{got['fileId']}/file")]
    if len(mine) != 1:
        raise Fail(f"GET /files lists {len(mine)} entries for the file {got['fileId']} the adaptor reported")
    if mine[0].get("fileFormat") != "json" or not isinstance(mine[0].get("fileSize"), int) or mine[0]["fileSize"] <= 0:
        raise Fail(f"the listed file reads {mine[0]}: expected fileFormat json and a size above 0")


@check("PM-3", "PM", "the stored file holds what was reported: its element, its counter and the cells measured", "FILE")
def pm_file_content(ctx: Context) -> None:
    """PM-3: the stored file, downloaded from RAN NF OAM, holds the element, the counter and exactly the cells that were reported."""
    ref, _ = _element(ctx)
    got, counter = _file(ctx, ["301", "302", "303"])
    oam = _oam_client(ctx)
    resp = oam.get(f"/pm-files/{got['fileId']}/file")
    if resp.status_code != 200:
        raise Fail(f"downloading the file answered {resp.status_code}")
    try:
        content = resp.json()
        cells = sorted(m["cellId"] for m in content["measurements"])
    except (ValueError, KeyError, TypeError):
        raise Fail("the stored file is not the reported measurements as JSON") from None
    if content.get("managedElementRef") != ref or content.get("counterType") != counter or cells != ["301", "302", "303"]:
        raise Fail(f"the stored file holds element {content.get('managedElementRef')!r}, counter {content.get('counterType')!r}, cells {cells}; "
                   f"reported {ref!r}, {counter!r}, ['301', '302', '303']")


# ------------------------------------------------------------------------------------------------------------ software management

SW_PACKAGE = "sftp://conformance.invalid/sw/conformance-1.0.zip"     # the package each check's job names; the adaptor is only asked to accept the RPC

def _start_job(ctx: Context) -> str:
    """Start a software-management job on the run's element at RAN NF OAM; returns its id, and fails unless a new job reads IN_PROGRESS in phase DOWNLOAD
    (RAN NF OAM has sent the adaptor `software-download` and the adaptor accepted it, call flow 19b)."""
    ref, _ = _element(ctx)
    job = _oam(ctx, "POST", "/software-management-jobs", "starting a software-management job",
               params={"managed_element_ref": ref, "remote_file_path": SW_PACKAGE})
    if job.get("status") != "IN_PROGRESS" or job.get("phase") != "DOWNLOAD":
        raise Fail(f"a new software-management job reads {job.get('status')}/{job.get('phase')}, not IN_PROGRESS/DOWNLOAD")
    return job["jobId"]


def _abort(ctx: Context, job_id: str) -> None:
    """End a job the check left running, so the next check may start one on the same element (one software job per element at a time)."""
    _oam(ctx, "POST", f"/software-management-jobs/{job_id}/abort", "aborting the software-management job", params={"reason": "conformance check done"})


def _job(ctx: Context, job_id: str) -> dict:
    """The software-management job `job_id` as RAN NF OAM lists it, or `Fail` when it is not listed."""
    ref, _ = _element(ctx)
    for job in _items(ctx, "/software-management-jobs", "reading the software-management jobs", {"managed_element_ref": ref}):
        if job.get("jobId") == job_id:
            return job
    raise Fail(f"RAN NF OAM does not list the software-management job {job_id}")


def _report_phase(ctx: Context, job_id: str, succeeded: bool) -> None:
    """Have the adaptor report the result of the job's current phase (`succeeded` true or false) to RAN NF OAM."""
    _trigger(ctx, "software-phase", {"jobId": job_id, "succeeded": succeeded})


@check("SW-1", "SW", "a phase result the adaptor reports moves the job on: DOWNLOAD done is read back as phase INSTALL, still IN_PROGRESS", "SWM")
def sw_download_done(ctx: Context) -> None:
    """SW-1: a successful DOWNLOAD result moves the job to phase INSTALL, still IN_PROGRESS."""
    job_id = _start_job(ctx)
    _report_phase(ctx, job_id, True)
    job = _job(ctx, job_id)
    _abort(ctx, job_id)
    if (job.get("phase"), job.get("status")) != ("INSTALL", "IN_PROGRESS"):
        raise Fail(f"after the adaptor reported the DOWNLOAD phase the job reads phase {job.get('phase')}, status {job.get('status')}; expected INSTALL, IN_PROGRESS")


@check("SW-2", "SW", "reporting DOWNLOAD, INSTALL and ACTIVATE in turn completes the job (COMPLETED, phase ACTIVATE)", "SWM")
def sw_completes(ctx: Context) -> None:
    """SW-2: three successful phase results walk the job INSTALL, ACTIVATE, then ACTIVATE/COMPLETED."""
    job_id = _start_job(ctx)
    seen = []
    for _ in range(3):
        _report_phase(ctx, job_id, True)
        job = _job(ctx, job_id)
        seen.append(f"{job.get('phase')}/{job.get('status')}")
    if seen != ["INSTALL/IN_PROGRESS", "ACTIVATE/IN_PROGRESS", "ACTIVATE/COMPLETED"]:
        raise Fail(f"after each of the three phase results the job read {seen}; expected INSTALL/IN_PROGRESS, ACTIVATE/IN_PROGRESS, ACTIVATE/COMPLETED")


@check("SW-3", "SW", "a failed phase the adaptor reports ends the job FAILED", "SWM")
def sw_fails(ctx: Context) -> None:
    """SW-3: a failed phase result (after one success) ends the job FAILED."""
    job_id = _start_job(ctx)
    _report_phase(ctx, job_id, True)
    _report_phase(ctx, job_id, False)
    job = _job(ctx, job_id)
    if job.get("status") != "FAILED":
        raise Fail(f"after the adaptor reported a failed INSTALL phase the job reads {job.get('phase')}/{job.get('status')}, not FAILED")


# ------------------------------------------------------------------------------------------------------------ heartbeat

@check("HB-1", "HB", "a heartbeat from the adaptor is recorded (lastHeartbeatAt) and the endpoint is ACTIVE (a DISCOVERED one becomes ACTIVE)", "HEARTBEAT")
def hb_marks_active(ctx: Context) -> None:
    """HB-1: a heartbeat changes the endpoint's `lastHeartbeatAt` and leaves it ACTIVE (a DISCOVERED endpoint becomes ACTIVE)."""
    ref, endpoint_id = _element(ctx)
    before = _endpoint_of(ctx, ref)
    _trigger(ctx, "heartbeat", {"endpointId": endpoint_id})
    after = _endpoint_of(ctx, ref)
    if not after.get("lastHeartbeatAt") or after.get("lastHeartbeatAt") == before.get("lastHeartbeatAt"):
        raise Fail(f"after the heartbeat the endpoint's lastHeartbeatAt is {after.get('lastHeartbeatAt')} (it was {before.get('lastHeartbeatAt')})")
    if after.get("healthStatus") != "ACTIVE":
        raise Fail(f"after the heartbeat the endpoint is {after.get('healthStatus')} (it was {before.get('healthStatus')}), not ACTIVE")


@check("HB-2", "HB", "a second heartbeat is recorded too: lastHeartbeatAt moves on and the endpoint stays ACTIVE", "HEARTBEAT")
def hb_repeats(ctx: Context) -> None:
    """HB-2: a second heartbeat moves `lastHeartbeatAt` on again (compared as ISO-8601 strings) and the endpoint stays ACTIVE."""
    ref, endpoint_id = _element(ctx)
    _trigger(ctx, "heartbeat", {"endpointId": endpoint_id})
    first = _endpoint_of(ctx, ref)
    _trigger(ctx, "heartbeat", {"endpointId": endpoint_id})
    second = _endpoint_of(ctx, ref)
    if not second.get("lastHeartbeatAt") or second["lastHeartbeatAt"] <= (first.get("lastHeartbeatAt") or ""):
        raise Fail(f"lastHeartbeatAt did not move on: {first.get('lastHeartbeatAt')} then {second.get('lastHeartbeatAt')}")
    if second.get("healthStatus") != "ACTIVE":
        raise Fail(f"after two heartbeats the endpoint is {second.get('healthStatus')}, not ACTIVE")
