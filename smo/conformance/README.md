# Conformance kits (`conformance/`)

| Kit | For | Run |
|---|---|---|
| `conformance/o1` | a vendor or integrator with an O1 adaptor | `python -m conformance.o1 --adaptor URL`; below |
| `conformance/rapp` | a rApp developer with a package (CSAR), and an operator who wants to know a platform treats a rApp's lifecycle as it should | `python -m conformance.rapp package my-rapp.csar`, `python -m conformance.rapp runtime --package-url URL --direct`; [`rapp/README.md`](rapp/README.md) |

Both write `report.json` and `report.md` with `--out`, exit 1 when a check fails, and run in CI against this repository's own mock adaptor and sample packages.

# O1 adaptor conformance kit (`conformance/o1`)

What RAN NF OAM relies on when it talks to an O1 adaptor, as checks you run against one. Written for a vendor or an integrator who has an adaptor and wants to know, before registering it, whether RAN NF OAM can use it; and for this repository, which runs it against its own mock adaptor on every pull request (CI job `o1-conformance`).

```bash
python -m conformance.o1 --adaptor http://adaptor:8000 --out report        # report.json and report.md
python -m conformance.o1 --adaptor http://adaptor:8000 --protocol netconf   # one transport; the default, auto, is what /capabilities declares
python -m conformance.o1 --adaptor http://adaptor:8000 --only NC-2 --only RESTCONF
python -m conformance.o1 --adaptor http://gateway --netconf-path /vendor/netconf --header "Authorization: Bearer ..."   # an adaptor not at the default paths
python -m conformance.o1 --adaptor x --list
# the other direction too (FM, PM, SW, HB): the adaptor emits to RAN NF OAM, the kit reads RAN NF OAM back
python -m conformance.o1 --adaptor http://adaptor:8000 --oam-url http://ran-nf-oam:8000
python -m conformance.o1 --adaptor http://adaptor:8000 --emit-url http://adaptor-triggers:9000 --oam-url http://ran-nf-oam:8000 --only FM --only HB
python -m conformance.o1 --adaptor http://adaptor:8000 --oam-url http://ran-nf-oam:8000 --element gnb-du-01   # an element that is already registered
```

Needs `httpx` and `defusedxml` (the SMO's runtime requirements). Exit status 0: every check that ran passed; 1: a check failed (the report says which and what the adaptor did instead).

| Option | Meaning |
|---|---|
| `--adaptor URL` | the adaptor's origin (required): the CM checks, `/capabilities`, and the trigger API unless `--emit-url` |
| `--oam-url URL` | RAN NF OAM's origin. The emitting groups (FM, PM, SW, HB) need it: the kit prepares an element there and reads back what arrived. Without it they are **skipped, and the report says so** (`give --oam-url`) |
| `--emit-url URL` | where the trigger API is, if not on the adaptor (default: `--adaptor`); `--emit-prefix` (default `/emit`) is the path the routes are under |
| `--element REF` | use a managed element that is already registered at RAN NF OAM with its O1 adaptor endpoint; default: the kit registers `conf-me-<run>-<n>` (with the services the adaptor declares) |
| `--protocol`, `--netconf-path`, `--restconf-root`, `--capabilities-path`, `--header`, `--timeout`, `--only`, `--out`, `--list` | as above; `--only` takes a check id or a group (`DISC`, `NETCONF`, `RESTCONF`, `FM`, `PM`, `SW`, `HB`) |

## What is checked, and what is not

The checks cover the contract RAN NF OAM's clients actually use: `ran-nf-oam/app/netconf_client.py` (an `<rpc><edit-config>` / `<get-config>` posted as XML to the adaptor's registered URI), `restconf_client.py` (RFC 8040 data resources under a RESTCONF root) and the vendor discovery that reads `GET /capabilities`. A request RAN NF OAM never makes is not tested: this is not a NETCONF or RESTCONF test suite, and an adaptor that passes it can still be unfit for something the SMO does not use it for. Each check is one sentence in the table below, and a failure says what the adaptor answered.

A check that is acknowledged-but-not-applied is the commonest real defect (an agent that says `<ok/>` and keeps nothing), so every write is read back.

| Check | Group | What |
|---|---|---|
| DISC-1 | DISC | GET /capabilities answers a JSON object with vendorName, supportedServices and supportedVendorModes |
| DISC-2 | DISC | every declared service and transport is one RAN NF OAM knows |
| DISC-3 | DISC | a transport that is declared is one this run exercises (and the declaration covers the ones it does) |
| NC-1 | NETCONF | an edit-config (merge) of a managed object is acknowledged with <ok/> |
| NC-2 | NETCONF | what an edit-config wrote is what get-config reads back (a write that is acknowledged and not applied is the commonest lie) |
| NC-3 | NETCONF | a second merge keeps the attributes the first one wrote |
| NC-4 | NETCONF | a managed function is addressed apart from its element (function-ref) |
| NC-5 | NETCONF | a delete removes what was written |
| NC-6 | NETCONF | a merge that carries no attribute is refused with an <rpc-error>, not acknowledged |
| NC-7 | NETCONF | a request with no managed-object ref is refused with an <rpc-error> |
| NC-8 | NETCONF | a body that is not XML is answered, not crashed on (no 5xx) |
| NC-9 | NETCONF | the reply carries the request's message-id |
| NC-10 | NETCONF | values with XML special characters round-trip, escaped |
| NC-11 | NETCONF | an XML entity in a request is not expanded (no entity expansion, no 5xx) |
| RC-1 | RESTCONF | /.well-known/host-meta names the RESTCONF root |
| RC-2 | RESTCONF | a PUT of a new object answers 201 and a PUT of an existing one answers 204 |
| RC-3 | RESTCONF | a GET reads back what a PUT wrote, as a list entry keyed by the object |
| RC-4 | RESTCONF | a PATCH merges: it changes what it names and keeps the rest |
| RC-5 | RESTCONF | a PUT replaces: attributes it does not name are gone |
| RC-6 | RESTCONF | a managed function is a resource below its element |
| RC-7 | RESTCONF | a DELETE answers 204 and a second DELETE answers 404 |
| RC-8 | RESTCONF | a body whose key does not match the resource is refused with 400 |
| RC-9 | RESTCONF | a body that is not a list entry is refused with 400 and an ietf-restconf:errors body |
| RC-10 | RESTCONF | a PATCH that names no attribute is refused with 400 |
| FM-1 | FM | an alarm the adaptor raises is accepted by RAN NF OAM's ingest, which answers 200 with an alarmId |
| FM-2 | FM | RAN NF OAM lists the alarm with the fields the adaptor sent (cause, problem, type, correlation group) |
| FM-3 | FM | the severity is taken as a PerceivedSeverity whatever its case: listed in lower case, with the upper-case perceivedSeverity |
| FM-4 | FM | the alarmId is minted by RAN NF OAM: a UUID that is not the adaptor's own id, new for each alarm; a repeat of an open alarm is that alarm |
| PM-1 | PM | after a PM subscription on the element, a report of its counter is accepted (201) and its measurements are counted |
| PM-2 | PM | a performance file the adaptor reports is accepted (201) and listed by GET /files with its format and a size |
| PM-3 | PM | the stored file holds what was reported: its element, its counter and the cells measured |
| SW-1 | SW | a phase result the adaptor reports moves the job on: DOWNLOAD done is read back as phase INSTALL, still IN_PROGRESS |
| SW-2 | SW | reporting DOWNLOAD, INSTALL and ACTIVATE in turn completes the job (COMPLETED, phase ACTIVATE) |
| SW-3 | SW | a failed phase the adaptor reports ends the job FAILED |
| HB-1 | HB | a heartbeat from the adaptor is recorded (lastHeartbeatAt) and the endpoint is ACTIVE (a DISCOVERED one becomes ACTIVE) |
| HB-2 | HB | a second heartbeat is recorded too: lastHeartbeatAt moves on and the endpoint stays ACTIVE |

### The emitting groups: FM, PM, SW, HB

These run the other way: the adaptor sends, RAN NF OAM receives (`POST /alarms/ingest`, `/pm-reports`, `/pm-files`, `/software-management-jobs/{id}/advance`, `/o1-adaptor-endpoints/{id}/heartbeat`). There is no standard way to ask an adaptor to do that, so the kit uses a **trigger API** that the adaptor under test provides. It is one `POST` per kind, with a JSON body, under `--emit-prefix` (default `/emit`):

| Trigger | Body | Answers | The adaptor then sends |
|---|---|---|---|
| `/emit/alarm` | `managedElementRef`, `severity`, `sourceAlarmId?`, `probableCause?`, `specificProblem?`, `managedFunctionRef?`, `alarmType?`, `correlationGroup?` | `{emitted, status, response}` | `POST /alarms/ingest` (the fields as query parameters) |
| `/emit/pm-report` | `managedElementRef`, `counterType`, `measurements[{cellId, timestamp, value?, values?, relation?}]` | same | `POST /pm-reports` |
| `/emit/pm-file` | the same, plus `fileDataType?`, `fileFormat?`, ... | same | `POST /pm-files` |
| `/emit/heartbeat` | `endpointId` | same | `POST /o1-adaptor-endpoints/{id}/heartbeat` |
| `/emit/software-phase` | `jobId`, `succeeded` | same | `POST /software-management-jobs/{id}/advance?succeeded=` |

`emitted` is only the adaptor's word; **every check reads RAN NF OAM's state afterwards** (`GET /alarms`, `/files`, the file itself, `/software-management-jobs`, `/o1-adaptor-endpoints`), so an adaptor that says `emitted: true` and sends nothing, sends the wrong severity or never reports a phase fails. The one exception to "read back" is PM-1: a report is not stored by RAN NF OAM (it goes to DME), so the check is RAN NF OAM's own answer to the report (201, and the number of measurements it counted); a file (PM-2, PM-3) is stored, and read back.

What the kit prepares at RAN NF OAM itself, once per run and with `--oam-url`: a managed element and its O1 adaptor endpoint (`POST /o1-adaptor-endpoints`, `supportedServices` = what the adaptor declares, or `--element` for one that exists), a PM subscription for a counter new to the run (`ConfPm<run>`), and a software-management job per SW check. The PM groups need more of the platform than RAN NF OAM: a PM subscription registers a DME type through R1, so a RAN NF OAM running alone (a process on SQLite) answers 500 to the subscription and PM-1 to PM-3 fail with that; FM, SW and HB need RAN NF OAM only (`--only FM --only SW --only HB`). A group is **skipped, not failed**, when the adaptor does not declare its service in `/capabilities` (`FM` for FM, `PM` for PM-1, `FILE` for PM-2 and PM-3, `SWM` for SW, `HEARTBEAT` for HB), or when `--oam-url` is missing; the report says which.

Each run uses managed-object references that are new to it (`conf-<run>-<n>`), so a second run against the same adaptor does not meet the first one's objects; the kit does not remove what it created (a RESTCONF `DELETE` and a NETCONF `delete` are themselves checked, and the other objects stay).

## Not covered yet

* **Anything the adaptor emits that RAN NF OAM does not take in**: streaming data, file notifications to a subscriber, clearing and acknowledging alarms. The emitting groups cover the five routes above. RAN NF OAM does take in VES (`POST /ves/eventListener/v7`, `PR-SB-7`), and the kit does not check an adaptor's VES: there is no trigger for it in the stub, and its tests (`ran-nf-oam/tests/test_ves.py`) are the receiver's, not a sender's.
* **A real vendor's trigger API.** The kit's emit side is the contract `mock-o1-adaptor` implements; a vendor adaptor needs a test hook of the same shape (or a thin shim in front of it) to be run through FM, PM, SW and HB. Without one, give no `--oam-url` and run the CM checks only.
* **NETCONF over SSH or TLS** (the transports RAN NF OAM also speaks, `PR-SB-1`): tested against a real server (netopeer2) in CI job `netconf-lab`, not by this kit, which speaks the HTTP-carried form the mock and a vendor's HTTP front end use.
* **YANG validation** of what an adaptor accepts (`PR-SB-5`).

## A vendor profile (`SB-10`)

The stub can be made one vendor's adaptor: `MOCK_O1_PROFILE=<name>` reads `mock-o1-adaptor/app/profiles/<name>/` (the vendor's name, services, transports, YANG, the descriptor generated from it, the class defaults). The kit run against the stub in a profile, with RAN NF OAM read back, is the profile's `conformance-report.md`, and `tests_integration/test_vendor_profile.py` fails when the committed report and a fresh run differ. The one shipped, `example-du`, is a stand-in, not a real vendor (no vendor lab was available): 23 checks pass, RESTCONF and the software checks are skipped because the profile does not declare them. A real vendor's run replaces it, and needs the vendor's simulator or lab for what the stub cannot show. `mock-o1-adaptor/app/profiles/example-du/README.md` lists the deviations.

## The stub

`mock-o1-adaptor` is the RAN O1 stub of this repository: it answers both forms, declares a vendor, can inject faults (`POST /faults`: a timeout, an `<rpc-error>`, an acknowledged write that is not applied), **emits** FM, PM, SW and heartbeat to RAN NF OAM on request (`POST /emit/...`, target `MOCK_O1_OAM_URL`) and passes this kit. Running the kit against it with a fault injected is how the CM checks are tested (`mock-o1-adaptor/tests/test_conformance_kit.py`: a write that is acknowledged and not applied fails NC-2 and RC-3). The emitting groups are tested with the stub wired to the real RAN NF OAM app (`tests_integration/test_o1_conformance_emit.py`), with fakes that each break one thing (says `emitted` and sends nothing, wrong severity, never reports the software phase), and in CI against the compose stack (job `compose-e2e`, step "O1 conformance kit, emitting side").

Limit: the stub's emit calls are plain HTTP. With mutual TLS between the services (`SMO_MTLS=on`) RAN NF OAM wants a client certificate the stub does not present, so its triggers answer 502; run the emitting groups on a stack without mTLS.
