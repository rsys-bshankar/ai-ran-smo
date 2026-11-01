#!/usr/bin/env python3
"""The CI job "O-RAN-SC simulator" (plan task S0.15, ADR 0012): what reaches RAN NF OAM from the simulated O-DUs of the compose profile `oran-sim`.

Runs in a container on the compose network (it addresses RAN NF OAM and the simulators by address), after the job has copied each simulator's own log
(`/var/log/pynts.log`, at DEBUG it holds every VES event the simulator posted) to a file:

    python scripts/oran_sim_check.py --oam-url http://ran-nf-oam:8000 --sim-log sim-1.log --sim-log sim-2.log [--summary FILE]

Environment: `NETCONF_CRED_ORANSIM_PASSWORD` (the simulators' NETCONF password, as RAN NF OAM has it) and `RAN_NF_OAM_VES_PASSWORD` (the listener's).
`PYTHONPATH=ran-nf-oam:shared`: the NETCONF steps use RAN NF OAM's own session wrapper (`app.netconf_ssh`).

Each step ends PASS, FAIL, XFAIL or XPASS. XFAIL is a step that is expected to fail until the PR-CONV row it names lands: today's code cannot do it yet,
and the step stays in the job so the job names what is missing (no module code is changed to make a step pass; that is Stage 3). XPASS is such a step
that passed: it fails the run, so the marking is removed in the PR that made it pass. The exit status is 0 only with no FAIL and no XPASS.

  registration  R1 each simulator's pnfRegistration reached RAN NF OAM's VES listener and was accepted (2xx)
                R2 XFAIL S8.2: RAN NF OAM made a managed element of it (today the listener answers pnfRegistration IGNORED DOMAIN_NOT_MAPPED)
                R3 the operator registers the element instead (POST /o1-adaptor-endpoints, with the address, port and user the pnfRegistration
                   carried, and credentialRef `oransim`) and pins its SSH host key (PUT .../host-keys)
  a) NETCONF    A1 RAN NF OAM's NETCONF client opens a session to the simulator with the pinned key; the simulator serves the 3GPP NRM modules
                A2 the same client reads the ManagedElement with get-config: a GNBDUFunction with an NRCellDU
                A3 XFAIL S3.3: getMOIAttributes of the NRCellDU through RAN NF OAM's ProvMnS (no ProvMnS facade and no 3GPP YANG profile yet)
                A4 XFAIL S3.3: modifyMOIAttributes of its administrativeState through RAN NF OAM's ProvMnS
                A5 XFAIL S3.3: the new value is in the simulator's running datastore (read back over NETCONF)
  b) alarm      B1 XFAIL S3.15: the simulator posted an alarm as VES stndDefined, namespace 3GPP-FaultSupervision (the m-release O-DU sends none,
                   ADR 0012 section 4: its fault management is not started and sends NETCONF notifications only)
                B2 XFAIL S3.15: an alarm of the simulator is in RAN NF OAM's alarm list (stndDefined fault supervision is not mapped yet)
  c) PM         C1 the simulator posted notifyFileReady as VES stndDefined (3GPP-PerformanceAssurance) and RAN NF OAM's listener accepted it (2xx)
                C2 the file it names is there over SFTP, with the credentials of its URL, and holds measurement values (the simulator side of S3.16)
                C3 XFAIL S3.16: RAN NF OAM fetched the file and lists it (GET /files, Performance) since the run started
                C4 XFAIL S3.16: the file RAN NF OAM holds carries the simulator's counters

For each VES event kind the simulators sent, the answer RAN NF OAM gives it today is printed too (the event, as the simulator wrote it, posted once
more to the listener): a diagnosis of the XFAIL steps, never a pass condition.
"""

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import httpx
from defusedxml import ElementTree as SafeET

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
ME_NS = "urn:3gpp:sa5:_3gpp-common-managed-element"
DU_NS = "urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction"
CELL_NS = "urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu"
YANG_LIBRARY = "urn:ietf:params:xml:ns:yang:ietf-yang-library"
PROVMNS = "/ProvMnS/19.2.0"                     # the one ProvMnS form and version of the bundle (specs/smo-schema-bundle/openapi/common/provmns-root.yaml)
CREDENTIAL = "oransim"
POST_LINE = re.compile(r"\] ves: POST (?P<url>\S+) -> (?P<body>\{.*\})")
FAILED_LINE = re.compile(r"request failed with status code|Could not send")


@dataclass
class Posted:
    """One VES event a simulator posted: its body, and whether the simulator logged a failure for it (a non-2xx answer or no answer)."""
    url: str
    event: dict
    failed: bool = False

    @property
    def header(self) -> dict:
        return self.event.get("event", {}).get("commonEventHeader", {})

    @property
    def kind(self) -> str:
        """`pnfRegistration`, or `stndDefined/<namespace>`, or the domain."""
        domain = self.header.get("domain", "")
        return f"{domain}/{self.header.get('stndDefinedNamespace', '')}" if domain == "stndDefined" else domain


@dataclass
class Simulator:
    """What one simulator's log says about it: the events it posted, its hostname (the VES sourceName of its PM and alarms) and its registration."""
    log: str
    posted: list[Posted] = field(default_factory=list)

    def of(self, kind: str) -> list[Posted]:
        return [p for p in self.posted if p.kind == kind]

    @property
    def ssh_registration(self) -> dict | None:
        """The `pnfRegistrationFields` of the registration for the SSH endpoint, the one RAN NF OAM can use (ADR 0012 section 5)."""
        for p in self.of("pnfRegistration"):
            fields = p.event["event"].get("pnfRegistrationFields", {})
            if fields.get("additionalFields", {}).get("protocol") == "SSH":
                return fields
        return None

    @property
    def hostname(self) -> str | None:
        """The element's name: the sourceName of its fileReady, or of its registration without the `_<port>` the simulator adds when it has two endpoints."""
        for p in self.of("stndDefined/3GPP-PerformanceAssurance"):
            return p.header.get("sourceName")
        for p in self.of("pnfRegistration"):
            return re.sub(r"_\d+$", "", p.header.get("sourceName", "")) or None
        return None


def parse_log(path: str) -> Simulator:
    """The VES events of one simulator log, in order; a failure line marks the event posted just before it as failed."""
    sim = Simulator(log=path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip()
            m = POST_LINE.search(line)
            if m:
                try:
                    sim.posted.append(Posted(m.group("url"), json.loads(m.group("body"))))
                except json.JSONDecodeError:
                    continue
            elif FAILED_LINE.search(line) and sim.posted:
                sim.posted[-1].failed = True
    return sim


class Run:
    """The steps of one run and their results; `step` records one, `table` is the Markdown summary."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def step(self, sid: str, title: str, ok: bool, detail: object = "", expected_fail: str | None = None) -> bool:
        """Records step `sid`. With `expected_fail` (a PR-CONV row and why) a failure is XFAIL and a pass is XPASS; without it, PASS or FAIL."""
        if expected_fail:
            status = "XPASS" if ok else "XFAIL"
        else:
            status = "PASS" if ok else "FAIL"
        note = f"expected to fail until {expected_fail}" if expected_fail else ""
        text = str(detail)[:600]
        print(f"{status:5} {sid} {title}" + (f" -- {note}" if note else "") + (f"\n      {text}" if text else ""))
        self.rows.append((sid, status, title, note or text.replace("|", "/")[:200]))
        return ok

    def failed(self) -> list[str]:
        return [f"{sid} {status}" for sid, status, _, _ in self.rows if status in ("FAIL", "XPASS")]

    def table(self) -> str:
        """The steps as a Markdown table (step, result, what, why or detail), for the job summary."""
        lines = ["| Step | Result | What | Why / detail |", "|---|---|---|---|"]
        lines += [f"| {sid} | {status} | {title} | {note} |" for sid, status, title, note in self.rows]
        return "\n".join(lines)


def _rpc(session, body: str):
    return session.rpc(f'<rpc message-id="sim" xmlns="{NC}">{body}</rpc>')


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_cell(session) -> tuple[dict, set[str]]:
    """(cell, modules): the first NRCellDU of the running datastore as {me, du, cell, administrativeState}, and the module names of ietf-yang-library."""
    modules = {(n.text or "").strip() for n in _rpc(session, f'<get><filter type="subtree"><yang-library xmlns="{YANG_LIBRARY}"/></filter></get>').iter()
               if _local(n.tag) == "name"}
    reply = _rpc(session, f'<get-config><source><running/></source><filter type="subtree"><ManagedElement xmlns="{ME_NS}"/></filter></get-config>')
    for me in (n for n in reply.iter() if _local(n.tag) == "ManagedElement"):
        for du in (n for n in me if _local(n.tag) == "GNBDUFunction"):
            for cell in (n for n in du if _local(n.tag) == "NRCellDU"):
                state = next((a.text for a in cell.iter() if _local(a.tag) == "administrativeState"), None)
                ident = lambda node: next((c.text for c in node if _local(c.tag) == "id"), None)  # noqa: E731
                return {"me": ident(me), "du": ident(du), "cell": ident(cell), "administrativeState": state}, modules
    return {}, modules


def host_key(host: str, port: int) -> tuple[str, str]:
    """(key type, base64) of the server's SSH host key, read from the handshake (what an operator would have from the element's documentation)."""
    import socket

    import paramiko
    transport = paramiko.Transport(socket.create_connection((host, port), timeout=15))
    try:
        transport.start_client(timeout=15)
        key = transport.get_remote_server_key()
        return key.get_name(), key.get_base64()
    finally:
        transport.close()


def sftp_file(location: str) -> bytes:
    """The bytes of the file an `sftp://user:password@host:port/path` URL names, read with that URL's own credentials."""
    import paramiko
    url = urlsplit(location)
    transport = paramiko.Transport((url.hostname, url.port or 22))
    try:
        transport.connect(username=unquote(url.username or ""), password=unquote(url.password or ""))
        sftp = paramiko.SFTPClient.from_transport(transport)
        with sftp.open(url.path) as handle:
            return handle.read()
    finally:
        transport.close()


def counters_of(xml_bytes: bytes) -> dict[str, str]:
    """The counter names and values of a 3GPP measurement file (TS 32.435 measData: measTypes by `p`, measResults by `p`), or {} when it is not one."""
    try:
        root = SafeET.fromstring(xml_bytes)                      # a file from the simulator: parsed without entities or DTDs
    except ET.ParseError:
        return {}
    names = {t.get("p"): (t.text or "").strip() for t in root.iter() if _local(t.tag) == "measType"}
    values = {r.get("p"): (r.text or "").strip() for r in root.iter() if _local(r.tag) == "r"}
    return {names[p]: values[p] for p in names if p in values}


def diagnose(client: httpx.Client, sims: list[Simulator]) -> None:
    """Posts one event of each kind the simulators sent to RAN NF OAM's listener once more and prints its answer: why the XFAIL steps fail today."""
    seen: dict[str, Posted] = {}
    for sim in sims:
        for p in sim.posted:
            seen.setdefault(p.kind, p)
    password = os.environ.get("RAN_NF_OAM_VES_PASSWORD", "")
    for kind, p in sorted(seen.items()):
        try:
            r = client.post("/ves/eventListener/v7", json=p.event, auth=(os.environ.get("RAN_NF_OAM_VES_USERNAME", "ves"), password))
            results = r.json().get("results", []) if r.headers.get("content-type", "").startswith("application/json") else []
            print(f"      RAN NF OAM's answer to {kind}: {r.status_code} {[(x.get('outcome'), x.get('codes')) for x in results]}")
        except httpx.HTTPError as exc:
            print(f"      RAN NF OAM's answer to {kind}: no answer ({type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    """Runs the steps in the order of the module docstring against every simulator whose log was given; returns 0 only with no FAIL and no XPASS."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oam-url", default="http://ran-nf-oam:8000")
    parser.add_argument("--sim-log", action="append", required=True, help="a simulator's /var/log/pynts.log, once per simulator")
    parser.add_argument("--summary", help="also write the result table (Markdown) to this file")
    args = parser.parse_args(argv)
    started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
    run = Run()
    sims = [parse_log(p) for p in args.sim_log]
    client = httpx.Client(base_url=args.oam_url.rstrip("/"), timeout=30)
    os.environ.setdefault("NETCONF_SSH_KNOWN_HOSTS", "")
    from app.netconf_ssh import NetconfSession, NetconfSshError      # RAN NF OAM's own NETCONF client (PYTHONPATH=ran-nf-oam:shared)

    for n, sim in enumerate(sims, start=1):
        name = sim.hostname or f"simulator-{n}"
        reg = sim.ssh_registration
        print(f"\n=== {name} ({sim.log}): {len(sim.posted)} VES events posted, kinds {sorted({p.kind for p in sim.posted})}")
        # ------------------------------------------------------------ registration
        regs = sim.of("pnfRegistration")
        run.step(f"R1[{name}]", "pnfRegistration reached RAN NF OAM's VES listener and was accepted", bool(regs) and reg is not None and not regs[-1].failed,
                 f"{len(regs)} posted, the SSH one: {reg and {k: reg.get(k) for k in ('oamV4IpAddress', 'unitType', 'vendorName', 'serialNumber')}}")
        listed = client.get("/o1-adaptor-endpoints", params={"limit": 500}).json().get("items", [])
        address = reg.get("oamV4IpAddress") if reg else None
        # an element at the simulator's address that this check did not register itself (R3 registers with credentialRef `oransim`, also on an earlier run)
        made = [e for e in listed if address and address in str(e.get("adaptorUri", "")) and e.get("credentialRef") != CREDENTIAL]
        run.step(f"R2[{name}]", "RAN NF OAM made a managed element of the registration", bool(made), made[:1],
                 expected_fail="PR-CONV-S8.2 (pnfRegistration is answered IGNORED DOMAIN_NOT_MAPPED today)")
        if not reg or not sim.hostname:
            run.step(f"R3[{name}]", "the operator registers the element from what the pnfRegistration carried", False, "no SSH pnfRegistration in the log")
            continue
        extra = reg.get("additionalFields", {})
        port, user = int(extra.get("oamPort", 830)), extra.get("username", "netconf")
        uri = f"ssh://{user}@{address}:{port}"
        endpoint = next((e for e in listed if e.get("managedElementRef") == sim.hostname), None)
        if endpoint is None:
            r = client.post("/o1-adaptor-endpoints", json={"managedElementRef": sim.hostname, "adaptorUri": uri, "transport": "ssh", "credentialRef": CREDENTIAL,
                                                            "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF", "entityType": "O-DU",
                                                            "vendorName": None, "supportedServices": ["PROV", "FM", "PM", "FILE", "HEARTBEAT"]})
            endpoint = r.json() if r.status_code == 201 else None
        key_type, key = host_key(address, port)
        pinned = client.put(f"/o1-adaptor-endpoints/{endpoint['endpointId']}/host-keys", json={"keyType": key_type, "publicKey": key, "pinnedBy": "oran-sim-check"}) \
            if endpoint else None
        if not run.step(f"R3[{name}]", "the operator registers the element (POST /o1-adaptor-endpoints) and pins its SSH host key", bool(pinned) and pinned.status_code == 200,
                        f"{uri} credentialRef {CREDENTIAL}: {endpoint and endpoint.get('endpointId')}, pin {pinned and pinned.status_code}"):
            continue
        # ------------------------------------------------------------ a) NETCONF
        with tempfile.NamedTemporaryFile("w", suffix=".known_hosts", delete=False) as known:
            known.write(f"[{address}]:{port} {key_type} {key}\n")
        os.environ["NETCONF_SSH_KNOWN_HOSTS"] = known.name
        cell, modules, error = {}, set(), ""
        try:
            with NetconfSession(uri, timeout=30, credential_ref=CREDENTIAL) as session:
                cell, modules = read_cell(session)
        except NetconfSshError as exc:
            error = str(exc)
        wanted = {"_3gpp-common-managed-element", "_3gpp-nr-nrm-gnbdufunction", "_3gpp-nr-nrm-nrcelldu"}
        run.step(f"A1[{name}]", "RAN NF OAM's NETCONF client connects (SSH, pinned key, hello) and the simulator serves the 3GPP NRM modules",
                 not error and wanted <= modules, error or f"served: {sorted(wanted & modules)}")
        run.step(f"A2[{name}]", "RAN NF OAM's NETCONF client reads the ManagedElement with get-config: a GNBDUFunction with an NRCellDU",
                 bool(cell.get("cell")), cell or "no NRCellDU in the running datastore")
        dn = f"{PROVMNS}/ManagedElement={sim.hostname}/GNBDUFunction={cell.get('du')}/NRCellDU={cell.get('cell')}"
        r = client.get(dn, params={"scope": "BASE_ONLY", "attributes": "administrativeState"})
        got = r.json().get("attributes", {}) if r.status_code == 200 and "json" in r.headers.get("content-type", "") else {}
        run.step(f"A3[{name}]", "getMOIAttributes of the NRCellDU through RAN NF OAM (ProvMnS)", r.status_code == 200 and got.get("administrativeState") == cell.get("administrativeState"),
                 f"GET {dn}: {r.status_code}", expected_fail="PR-CONV-S3.3 (no ProvMnS facade and no 3GPP YANG profile in RAN NF OAM yet)")
        new = "UNLOCKED" if cell.get("administrativeState") == "LOCKED" else "LOCKED"
        r = client.patch(dn, content=json.dumps({"attributes": {"administrativeState": new}}), headers={"Content-Type": "application/merge-patch+json"})
        run.step(f"A4[{name}]", f"modifyMOIAttributes administrativeState={new} on the NRCellDU through RAN NF OAM (ProvMnS)", r.status_code in (200, 204),
                 f"PATCH {dn}: {r.status_code}", expected_fail="PR-CONV-S3.3 (no ProvMnS facade and no 3GPP YANG profile in RAN NF OAM yet)")
        after = {}
        try:
            with NetconfSession(uri, timeout=30, credential_ref=CREDENTIAL) as session:
                after, _ = read_cell(session)
        except NetconfSshError as exc:
            after = {"error": str(exc)}
        run.step(f"A5[{name}]", "the simulator's running datastore shows the new value (read back over NETCONF)", after.get("administrativeState") == new,
                 after, expected_fail="PR-CONV-S3.3 (the write above does not reach the simulator yet)")
        # ------------------------------------------------------------ b) alarm
        alarms_sent = sim.of("stndDefined/3GPP-FaultSupervision")
        run.step(f"B1[{name}]", "the simulator posted an alarm as VES stndDefined (3GPP-FaultSupervision)", bool(alarms_sent), f"{len(alarms_sent)} posted",
                 expected_fail="PR-CONV-S3.15 (and the m-release O-DU sends no VES alarm: ADR 0012 section 4)")
        alarms = client.get("/alarms", params={"managed_element_ref": sim.hostname, "limit": 50}).json().get("items", [])
        run.step(f"B2[{name}]", "an alarm of the simulator is in RAN NF OAM's alarm list", bool(alarms), f"{len(alarms)} alarms on {sim.hostname}",
                 expected_fail="PR-CONV-S3.15 (stndDefined fault supervision is not mapped by the VES listener yet)")
        # ------------------------------------------------------------ c) PM
        ready = sim.of("stndDefined/3GPP-PerformanceAssurance")
        accepted = [p for p in ready if not p.failed]
        data = accepted[-1].event["event"].get("stndDefinedFields", {}) if accepted else {}
        info = (data.get("data", {}).get("fileInfoList") or [{}])[0]
        run.step(f"C1[{name}]", "notifyFileReady reached RAN NF OAM's VES listener as stndDefined and was accepted", bool(accepted),
                 f"{len(ready)} posted, {len(accepted)} accepted; schemaReference {data.get('schemaReference')}; "
                 f"{data.get('data', {}).get('notificationType')} {re.sub(r'//[^@/]*@', '//***@', str(info.get('fileLocation')))}")
        counters, error = {}, ""
        try:
            counters = counters_of(sftp_file(info["fileLocation"])) if info.get("fileLocation") else {}
        except Exception as exc:  # noqa: BLE001 — any failure to fetch is the step's answer, reported by kind
            error = f"{type(exc).__name__}: {exc}"
        run.step(f"C2[{name}]", "the file it names is there over SFTP and holds measurement values", bool(counters), error or counters)
        files = client.get("/files", params={"fileDataType": "Performance", "beginTime": started.isoformat(), "limit": 500})
        stored = files.json().get("items", []) if files.status_code == 200 else []
        run.step(f"C3[{name}]", "RAN NF OAM fetched the file and lists it (GET /files, Performance)", bool(stored), f"{len(stored)} performance files since the run started",
                 expected_fail="PR-CONV-S3.16 (no file-ready handling and no SFTP pull in RAN NF OAM yet)")
        held = [client.get(f["fileLocation"].removeprefix("/ran-nf-oam")).text for f in stored]
        run.step(f"C4[{name}]", "the counters land where PM lands today (RAN NF OAM's stored file holds them)",
                 bool(counters) and any(all(c in text for c in counters) for text in held), f"{len(held)} files read",
                 expected_fail="PR-CONV-S3.16 (the file is not fetched yet)")

    print("\nRAN NF OAM's answers today, per VES event kind the simulators sent:")
    diagnose(client, sims)
    print("\n" + run.table())
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as out:
            out.write("### O-RAN-SC simulator (S0.15)\n\n" + run.table() + "\n")
    bad = run.failed()
    print("FAILED: " + ", ".join(bad) if bad else "OK: no step failed that is not expected to, and none passed that is expected to fail")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
