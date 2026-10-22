# Call Flow 19: Software management job — starting, sending each phase to the element, failure and abort, campaigns

How RAN NF OAM updates the software of one element (HISTORY.md SA-O1-3): a software management job runs the O-RAN WG4 M-Plane software management RPCs
(`o-ran-software-management.yang`: `software-download`, `software-install`, `software-activate`) against the element through its O1 adaptor, one phase at a
time, and the element reports each phase's outcome (the M-Plane's `download-event`, `install-event`, `activation-event`). Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 19a | Starting a job | Operator (GUI), an SMO module, a campaign wave | a running job for a package on a registered element, one at a time, or refused |
| 19b | Each phase sent to the element | RAN NF OAM after each commit, its worker as a backstop | the phase's RPC sent once, accepted, or the job failed with the element's reason |
| 19c | Reports, failure, abort, reading | the element's adaptor, Operator (GUI) | a job moved on, completed (its version recorded on the element) or failed with a reason |
| 19d | Jobs of a software campaign | a campaign (PR-MGT-15) | each wave's jobs sent and reported the same way, the campaign told of each end |

**As images:** [19a](images/19-software-management-job-lifecycle/19a-starting-a-job.png) ·
[19b](images/19-software-management-job-lifecycle/19b-each-phase-sent-to-the-element.png) ·
[19c](images/19-software-management-job-lifecycle/19c-reports-failure-abort-reading.png) ·
[19d](images/19-software-management-job-lifecycle/19d-jobs-of-a-software-campaign.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach RAN NF OAM through R1 Termination (or the GUI BFF).
A job has a `status` (`PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`) and a `phase` (`DOWNLOAD`, `INSTALL`, `ACTIVATE`): the phase of a failed job stays at
the phase that failed. Every status change is published as `org.oran.smo.software-job.state-changed` in the same commit (`docs/EVENTS.md`).

## 19a — Starting a job

```mermaid
sequenceDiagram
    participant C as Operator (GUI) / SMO module
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    C->>OAM: POST /software-management-jobs?managed_element_ref&remote_file_path&software_version?&ru_instance_id?
    alt the element outside the caller's scope claim
        OAM-->>C: 403 SCOPE_DENIED
    else the element not registered
        OAM-->>C: 404 MANAGED_ENTITY_NOT_FOUND
    else no remote_file_path, or one longer than 1000 characters
        OAM-->>C: 422
    else MSAC on and no exec on the element, or its vendor's services lack SWM
        OAM-->>C: 403 MSAC_ACCESS_DENIED / 409 O1_SERVICE_NOT_SUPPORTED
    else a job already PENDING or IN_PROGRESS on the element
        OAM-->>C: 409 SOFTWARE_JOB_IN_PROGRESS
    else
        Note over OAM: PENDING, then START: IN_PROGRESS, phase DOWNLOAD,<br/>the package's path and version kept on the job
        OAM->>DB: job, event software-job.state-changed (PENDING to IN_PROGRESS), one commit
        Note over OAM: after the commit, the DOWNLOAD RPC is sent (19b)
        OAM-->>C: 202 {jobId, status, phase, softwareVersion, remoteFilePath, dispatchedPhase, failureReason}
    end
```

The package was never named, so the element was told nothing to download, and nothing was sent to it at all: an operator advanced a job that only existed in
the SMO. An unknown element made a job, and two jobs could run on one element at once.

## 19b — Each phase sent to the element

```mermaid
sequenceDiagram
    participant OAM as RAN NF OAM (dispatch_due)
    participant DB as RAN NF OAM database
    participant AD as The element's O1 adaptor
    participant NE as The element

    Note over OAM: after the commit that started a job or moved its phase on,<br/>and every 15 s from the worker (advance-waves)
    OAM->>DB: the running jobs whose phase has not been sent (dispatched_phase is not phase)
    OAM->>DB: UPDATE dispatched_phase = phase where it is still unsent, committed
    alt another process claimed it, or the job moved on
        Note over OAM: nothing sent, so a phase is sent once
    else claimed
        OAM->>AD: NETCONF rpc: software-download {remote-file-path},<br/>software-install {slot-name standby, file-names}, or software-activate {slot-name standby}
        AD->>NE: the RPC
        alt rpc-reply ok
            AD-->>OAM: ok
            Note over OAM: the job waits for the element's report (19c)
        else rpc-error, no answer, no endpoint, endpoint UNREACHABLE or DEGRADED, element not on NETCONF
            AD-->>OAM: the error, or nothing
            OAM->>DB: PHASE_FAILED: FAILED, failureReason "software-download not accepted: ..." (the error tag and message),<br/>event software-job.state-changed, one commit
            Note over OAM: a campaign job's campaign is told (19d)
        end
    end
```

The RPC goes over the endpoint's transport: SSH or TLS through the NETCONF session (its pinned host keys for SSH), HTTP to the adaptor's NETCONF bridge.
**Phase 1:** the install always targets the `standby` slot and names the last segment of the remote path as the file. An element reached over RESTCONF is
not sent software RPCs (`PROTOCOL_NOT_SUPPORTED`). The adaptor's report of a phase's completion comes as a call to `advance` (19c), not as a NETCONF
notification RAN NF OAM subscribes to.

## 19c — Reports, failure, abort, reading

```mermaid
sequenceDiagram
    participant E as The element's adaptor / Operator (GUI)
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    E->>OAM: POST /software-management-jobs/{id}/advance?succeeded&reason?
    alt unknown job, or one on an element outside the caller's scope
        OAM-->>E: 404 SOFTWARE_JOB_NOT_FOUND
    else MSAC on and no exec on the element
        OAM-->>E: 403 MSAC_ACCESS_DENIED
    else the job has ended (completed, failed, timed out by its campaign, aborted)
        OAM-->>E: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else succeeded, phase DOWNLOAD or INSTALL
        OAM->>DB: phase INSTALL or ACTIVATE, one commit
        Note over OAM: after the commit, the next phase's RPC is sent (19b)
        OAM-->>E: 200 the job
    else succeeded, phase ACTIVATE
        OAM->>DB: COMPLETED, the element's softwareVersion set to the job's (not for a revert job),<br/>event software-job.state-changed, one commit
        OAM-->>E: 200 the job
    else succeeded=false
        OAM->>DB: FAILED, failureReason the reason given (or "the element reported the phase failed"),<br/>the phase kept, event software-job.state-changed, one commit
        OAM-->>E: 200 the job
    end
    E->>OAM: POST /software-management-jobs/{id}/abort?reason
    alt the job has ended
        OAM-->>E: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        OAM->>DB: FAILED, failureReason "aborted: reason", nothing sent to the element, one commit
        OAM-->>E: 200 the job
    end
    E->>OAM: GET /software-management-jobs/{id}, GET /software-management-jobs?managed_element_ref
    OAM-->>E: the job(s), as above (404 outside the caller's scope)
```

Anyone who could reach the route could advance a job (no MSAC check), a failure kept no reason, a completed job left the element's recorded version as it
was, and a job whose element never reported could only be left running. A failed job is not retried: a new job is started (the GUI's retry does that with
the same package).

## 19d — Jobs of a software campaign

```mermaid
sequenceDiagram
    participant O as Operator (GUI)
    participant OAM as RAN NF OAM (campaigns)
    participant AD as The elements' O1 adaptors

    O->>OAM: POST /software-campaigns {name, softwareVersion?, remoteFilePath?, elements or selector, waveSize, gate, jobTimeoutSeconds?, ...}
    alt an element of it already running a software job
        OAM-->>O: 409 SOFTWARE_JOB_IN_PROGRESS
    else
        OAM->>OAM: wave 1: one job per element with the campaign's remoteFilePath and softwareVersion, one commit
        OAM-->>O: 202 the campaign
        OAM->>AD: each job's software-download (19b)
    end
    AD->>OAM: each phase reported (19c), or a refused RPC fails the job (19b)
    Note over OAM: when a wave's jobs have all ended: the health gate, then the next wave's jobs (sent the same way),<br/>a halt, or on rollback one revert job per completed job (no remote path: the element reverts to its other slot)
    Note over OAM: jobTimeoutSeconds: a job not reported in time fails ("no report within N s"), its campaign decides as for any failed job
```

The campaign's jobs were sent nothing either, and a busy element was refused as `SERVICE_NAME_CONFLICT`. A campaign's own lifecycle (waves, gates, halts,
rollback order, notifications) is unchanged and described in `ran-nf-oam/README.md` (PR-MGT-15, MGT-15.7).

## Decisions this flow depends on

- **A job names its package and is sent to the element** (DOC-CF-1.8): `remote_file_path` is required, and each phase's RPC is sent to the element's O1
  adaptor after the commit that made it due, claimed so that it is sent once however many replicas run; the worker sends any phase not yet claimed (a crash after the claim and before
  the send leaves the phase claimed and unsent until the campaign's timeout or an abort, `OPEN_ITEMS.md` OAM-SWM-RESEND).
- **The element reports, the SMO does not guess**: a phase completes when the element (its adaptor) reports it through `advance`; a refused or unreachable RPC
  fails the job at once with the element's reason.
- **One job at a time per element, on a registered element** (DOC-CF-1.8): 409 `SOFTWARE_JOB_IN_PROGRESS`, 404 `MANAGED_ENTITY_NOT_FOUND`.
- **Reporting is as guarded as starting** (DOC-CF-1.8): MSAC `exec` on the element for start, advance and abort.
- **A completed job is the element's version** (DOC-CF-1.8): the element's `softwareVersion` is set when a job naming a version completes; every status change
  is on the event feed.
- **`phase` and `status` are separate**: a failed job's phase is the one that failed, and one `PHASE_FAILED` event fails any phase.
- **The vendor's services decide** (call flow 21): an element whose vendor does not implement SWM is refused `O1_SERVICE_NOT_SUPPORTED`.
- **`ru_instance_id`** is kept on the job, as the MnS hierarchy workbook asks for RU-level reuse of the RPCs.
- **Not taken:** subscribing to the element's NETCONF notifications (the adaptor reports through `advance`), choosing a slot other than `standby`, checksum or
  signature checks of the package in the SMO, and software RPCs over RESTCONF.
