# O1 adaptor conformance: mock-o1-adaptor in profile example-du, RAN NF OAM in the in-process mesh

Protocols run: netconf. RAN NF OAM read back from: ran-nf-oam in the in-process mesh. **23 passed, 0 failed, 13 skipped.**

| Check | Group | What | Result | |
|---|---|---|---|---|
| DISC-1 | DISC | GET /capabilities answers a JSON object with vendorName, supportedServices and supportedVendorModes | pass |  |
| DISC-2 | DISC | every declared service and transport is one RAN NF OAM knows | pass |  |
| DISC-3 | DISC | a transport that is declared is one this run exercises (and the declaration covers the ones it does) | pass |  |
| NC-1 | NETCONF | an edit-config (merge) of a managed object is acknowledged with <ok/> | pass |  |
| NC-2 | NETCONF | what an edit-config wrote is what get-config reads back (a write that is acknowledged and not applied is the commonest lie) | pass |  |
| NC-3 | NETCONF | a second merge keeps the attributes the first one wrote | pass |  |
| NC-4 | NETCONF | a managed function is addressed apart from its element (function-ref) | pass |  |
| NC-5 | NETCONF | a delete removes what was written | pass |  |
| NC-6 | NETCONF | a merge that carries no attribute is refused with an <rpc-error>, not acknowledged | pass |  |
| NC-7 | NETCONF | a request with no managed-object ref is refused with an <rpc-error> | pass |  |
| NC-8 | NETCONF | a body that is not XML is answered, not crashed on (no 5xx) | pass |  |
| NC-9 | NETCONF | the reply carries the request's message-id | pass |  |
| NC-10 | NETCONF | values with XML special characters round-trip, escaped | pass |  |
| NC-11 | NETCONF | an XML entity in a request is not expanded (no entity expansion, no 5xx) | pass |  |
| RC-1 | RESTCONF | /.well-known/host-meta names the RESTCONF root | skipped | restconf is not part of this run |
| RC-2 | RESTCONF | a PUT of a new object answers 201 and a PUT of an existing one answers 204 | skipped | restconf is not part of this run |
| RC-3 | RESTCONF | a GET reads back what a PUT wrote, as a list entry keyed by the object | skipped | restconf is not part of this run |
| RC-4 | RESTCONF | a PATCH merges: it changes what it names and keeps the rest | skipped | restconf is not part of this run |
| RC-5 | RESTCONF | a PUT replaces: attributes it does not name are gone | skipped | restconf is not part of this run |
| RC-6 | RESTCONF | a managed function is a resource below its element | skipped | restconf is not part of this run |
| RC-7 | RESTCONF | a DELETE answers 204 and a second DELETE answers 404 | skipped | restconf is not part of this run |
| RC-8 | RESTCONF | a body whose key does not match the resource is refused with 400 | skipped | restconf is not part of this run |
| RC-9 | RESTCONF | a body that is not a list entry is refused with 400 and an ietf-restconf:errors body | skipped | restconf is not part of this run |
| RC-10 | RESTCONF | a PATCH that names no attribute is refused with 400 | skipped | restconf is not part of this run |
| FM-1 | FM | an alarm the adaptor raises is accepted by RAN NF OAM's ingest, which answers 200 with an alarmId | pass |  |
| FM-2 | FM | RAN NF OAM lists the alarm with the fields the adaptor sent (cause, problem, type, correlation group) | pass |  |
| FM-3 | FM | the severity is taken as a PerceivedSeverity whatever its case: listed in lower case, with the upper-case perceivedSeverity | pass |  |
| FM-4 | FM | the alarmId is minted by RAN NF OAM: a UUID that is not the adaptor's own id, new for each alarm; a repeat of an open alarm is that alarm | pass |  |
| PM-1 | PM | after a PM subscription on the element, a report of its counter is accepted (201) and its measurements are counted | pass |  |
| PM-2 | PM | a performance file the adaptor reports is accepted (201) and listed by GET /files with its format and a size | pass |  |
| PM-3 | PM | the stored file holds what was reported: its element, its counter and the cells measured | pass |  |
| SW-1 | SW | a phase result the adaptor reports moves the job on: DOWNLOAD done is read back as phase INSTALL, still IN_PROGRESS | skipped | the adaptor does not declare SWM in supportedServices |
| SW-2 | SW | reporting DOWNLOAD, INSTALL and ACTIVATE in turn completes the job (COMPLETED, phase ACTIVATE) | skipped | the adaptor does not declare SWM in supportedServices |
| SW-3 | SW | a failed phase the adaptor reports ends the job FAILED | skipped | the adaptor does not declare SWM in supportedServices |
| HB-1 | HB | a heartbeat from the adaptor is recorded (lastHeartbeatAt) and the endpoint is ACTIVE (a DISCOVERED one becomes ACTIVE) | pass |  |
| HB-2 | HB | a second heartbeat is recorded too: lastHeartbeatAt moves on and the endpoint stays ACTIVE | pass |  |
