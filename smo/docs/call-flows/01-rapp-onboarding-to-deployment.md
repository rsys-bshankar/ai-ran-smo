# Call Flow 01: rApp onboarding → running instance

An operator onboards an rApp package, creates an instance of it, and the rApp that serves the instance bootstraps as that instance. Five
diagrams, each one step a person or a component takes:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 01a | Onboard a package | Operator | package AVAILABLE (or FAILED) |
| 01b | Create an instance | Operator | instance DEPLOYING, its identity made and pushed to its rApp |
| 01c | The rApp receives the instance's credentials | rApp Management (inside 01b) | the rApp holds them in its SDK |
| 01d | The rApp bootstraps as the instance | rApp code (`/start`) | instance RUNNING, its safeguards in force |
| 01e | Re-send the credentials (rApp unreachable at create, or a rotation) | Operator | the rApp holds a new secret for the same identity |

**As images:** [01a](images/01-rapp-onboarding-to-deployment/01a-onboard-a-package.png) ·
[01b](images/01-rapp-onboarding-to-deployment/01b-create-an-instance.png) ·
[01c](images/01-rapp-onboarding-to-deployment/01c-the-rapp-receives-the-instance-s-credentials.png) ·
[01d](images/01-rapp-onboarding-to-deployment/01d-the-rapp-bootstraps-as-the-instance.png) ·
[01e](images/01-rapp-onboarding-to-deployment/01e-re-send-the-credentials.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`; a test fails when a diagram changes and its image is not drawn again).

**How to read the diagrams.** An arrow is one call: method, route and the inputs that matter. A box (`Note over`) on the receiver says what it
does with them. An `alt` block is the outcome: the first branch is success, each `else` a failure and what the caller gets. Every operator call
goes from the console (GUI → GUI backend → R1 Termination) to the module; that path is the same everywhere and is not drawn: the actor
*Operator (GUI)* stands for it. An rApp's calls go through its SDK and R1 Termination, which checks the token at SME and stamps the caller's
role and invoker id; 01d draws that once.

**What this build does not do yet (Phase 1).** NFO records a deployment and answers RUNNING, but starts no workload: the sample rApps run as
fixed services of the compose file (or the Helm chart), each serving every instance of its package. That is why an rApp gets its instance's
credentials pushed to it (01c) instead of finding them in the environment of a workload started for the instance.

## 01a — Onboard a package

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    participant Onb as Onboarding
    participant Host as Package host
    participant NFO as NFO

    alt upload from the console (multipart, up to 50 MiB)
        Op->>Onb: POST /onboarding/packages/upload<br/>{file: name.csar, applicationType: rApp}
        Note over Onb: insert package ONBOARDING (committed)<br/>location recorded as upload:name.csar<br/>file name must end .csar
    else a location Onboarding fetches
        Op->>Onb: POST /onboarding/packages<br/>{location: http(s)://…/name.csar, applicationType: rApp}
        Note over Onb: insert package ONBOARDING (committed)<br/>location must end .csar and pass the SSRF guard
        Onb->>Host: GET location (30 s, no redirects)
        Host-->>Onb: CSAR bytes
    end
    Note over Onb: 1. signature over TOSCA-Metadata/DIGESTS.sha256<br/>   (ONBOARDING_TRUST_STORE, when set)<br/>2. TOSCA.meta → Entry-Definitions<br/>3. identity (name, version, vendor, descriptor ids)<br/>4. manifest: aiCapabilities, operatorApi.baseUrl,<br/>   operatorUi, limits, runtimeProfiles · Files/Sme<br/>5. Artifacts/ registered · integrity hash (SHA-256)<br/>6. same hash already onboarded → refused
    Onb->>NFO: POST /nfo/descriptors<br/>{packageId, name: entry definitions,<br/>workloadTemplate{toscaEntryDefinitions, containerResourcesByMode}}
    Note over NFO: store NFDeploymentDescriptor<br/>(FK to the package row committed above)
    alt every step passed and NFO answered 201
        NFO-->>Onb: 201 {nfDeploymentDescriptorId}
        Note over Onb: ONBOARDING → AVAILABLE<br/>keep descriptor id, identity, declarations
        Onb-->>Op: 202 {packageId, trackingId}
    else not .csar, location refused, fetch failed, signature, TOSCA, manifest or declaration invalid, duplicate hash
        Note over Onb: ONBOARDING → FAILED (NFO not called)
        Onb-->>Op: 202 {packageId, failureReason: place and rule broken}
    else NFO refused the descriptor
        NFO-->>Onb: 4xx/5xx
        Note over Onb: ONBOARDING → FAILED (identity kept for the record)
        Onb-->>Op: 202 {packageId, failureReason: DescriptorCreationFailed}
    else any other error (e.g. NFO's 201 carries no usable id)
        Note over Onb: work after the first commit rolled back<br/>ONBOARDING → FAILED (never left ONBOARDING)
        Onb-->>Op: 202 {packageId, failureReason: exception class}
    end
    Op->>Onb: GET /onboarding/packages/{id}/onboarding-status
    Onb-->>Op: {state AVAILABLE or FAILED, nfDeploymentDescriptorId, aiCapabilities, smeDeclarations}
```

The answer is 202 whatever the verdict: the verdict is the package's state. A body without `location` or with an unknown `applicationType`
is 422 and creates nothing.

## 01b — Create an instance

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    participant RM as rApp Management
    participant Onb as Onboarding
    participant SME as SME
    participant NFO as NFO
    participant FOCOM as FOCOM
    participant App as rApp (operatorApiBase)

    Op->>RM: POST /rapp-mgmt/instances<br/>{packageId, config, autonomyMode (default SHADOW),<br/>regionScope, operatorApiBase (optional), approvalPolicy, authzScope}
    Note over RM: operatorApiBase: http(s), no credentials,<br/>not loopback/link-local/metadata (else 422)<br/>approvalPolicy only with ASSIST (else 422)<br/>authzScope well-formed (else 422)
    RM->>Onb: GET /onboarding/packages/{packageId}/onboarding-status
    alt AVAILABLE or PRIMED, with nfDeploymentDescriptorId
        Onb-->>RM: {state, nfDeploymentDescriptorId,<br/>aiCapabilities.operatorApi.baseUrl, …}
    else unknown package
        Onb-->>RM: 404
        RM-->>Op: 404 PACKAGE_NOT_FOUND (nothing created)
    else any other state, or no descriptor
        RM-->>Op: 409 PACKAGE_NOT_DEPLOYABLE (nothing created)
    end
    Note over RM: new RAppInstance DEPLOYING<br/>(autonomyMode and regionScope fixed for its lifetime)
    RM->>Onb: POST /onboarding/packages/{packageId}/usage/start?consumer_id={id}
    Note over Onb: package still AVAILABLE or PRIMED?<br/>open a usage registration (call flow 06)
    alt registration opened
        Onb-->>RM: 200 {registrationId}
        Note over RM: keep packageUsageRegistrationId<br/>(the deprime and delete guards now see the instance)
    else package changed state since it was read
        Onb-->>RM: 409 PACKAGE_NOT_DEPLOYABLE
        RM-->>Op: 409 PACKAGE_NOT_DEPLOYABLE (nothing created)
    else Onboarding refused or did not answer
        RM-->>Op: 502 UPSTREAM_FAILED / 503 ENDPOINT_UNREACHABLE (nothing created)
    end
    RM->>SME: POST /sme/invoker-registrations<br/>{apiInvokerPublicKey: rapp-instance:{id}, authzScope}
    Note over SME: new invoker, kind rapp (no enrollment secret)<br/>keeps only the secret's hash
    alt registered, with the scope asked for
        SME-->>RM: 201 {apiInvokerId, onboardingSecret, authzScope}
        Note over RM: oauthClientId = apiInvokerId<br/>(limits, kill switch, approval policy, audit key on it)<br/>secret held for this request only, never stored
    else SME unreachable or refused, or scope not recorded
        RM->>Onb: POST …/usage/{registrationId}/stop (best-effort)
        RM-->>Op: 503 ENDPOINT_UNREACHABLE (invoker removed, registration stopped, nothing created)
    end
    RM->>NFO: POST /nfo/deployments<br/>{nfDeploymentDescriptorId, name: rapp-instance-{id}, requiredResourceTypeId}
    Note over NFO: descriptor exists (else 422) · name unused (else 409)<br/>(a descriptor backs one deployment per instance)
    NFO->>FOCOM: GET /focom/inventory?resource_type
    FOCOM-->>NFO: {oCloudId} (none, or no answer: NFO 503, call flow 15b)
    Note over NFO: deployment INITIAL → RUNNING in this request<br/>Phase 1: records it, starts no workload
    alt NFO instantiates
        NFO-->>RM: 202 {nfDeploymentId, state RUNNING, clusterId}
        Note over RM: workloadRef = nfDeploymentId
    else NFO refuses or does not answer
        RM-->>Op: 502 UPSTREAM_FAILED / 503 (registration stopped,<br/>invoker removed, nothing created)
    end
    Note over RM: operator API base = the operator's operatorApiBase,<br/>else the package's operatorApi.baseUrl, else none<br/>commit the instance (DEPLOYING)
    opt a base is known (01c)
        RM->>App: POST /rapps/{id}/operator/instances/{id}/credentials (via R1)<br/>{oauthClientId, oauthClientSecret}
        App-->>RM: 204 (or an error: not delivered)
    end
    RM-->>Op: 202 {instanceId, oauthClientId, credentialsDelivered}
```

The operator types no address for a package that declares one (`operatorApi.baseUrl` in its manifest, checked by Onboarding with the
`operatorApiBase` rule); a typed address overrides it. `credentialsDelivered: false` (no base at all, or the rApp did not take them) leaves the instance DEPLOYING; the rApp cannot bootstrap it until it
gets them: the operator sends them with **Re-send credentials** (01e) once the rApp is reachable.

## 01c — The rApp receives the instance's credentials

```mermaid
sequenceDiagram
    participant RM as rApp Management
    participant R1 as R1 Termination
    participant App as rApp (route from smo_sdk.instances)
    participant SDK as rApp SDK (R1Client)

    RM->>R1: POST /rapps/{id}/operator/instances/{id}/credentials<br/>{oauthClientId, oauthClientSecret} (rApp Mgmt's own token)
    Note over R1: token checked at SME → role internal<br/>resolve {id} → operatorApiBase (asks rApp Mgmt, cached 5 s)<br/>base passes the SSRF guard again
    alt base registered and reachable
        R1->>App: POST {base}/instances/{id}/credentials<br/>X-R1-Role: internal
        Note over App: accepted from role internal only (else 403)<br/>a browser cannot reach it: the GUI backend<br/>forwards only the routes the package declares
        App->>SDK: bind_instance_identity(id, oauthClientId, secret)
        Note over SDK: stored in the shared identity table<br/>(module_identity, key rapp-instance:{id}): every replica<br/>and a restarted process have it · a later push replaces it
        App-->>R1: 204
        R1-->>RM: 204 → credentialsDelivered true
    else no base, instance terminated
        R1-->>RM: 404 OPERATOR_API_NOT_REGISTERED → false
    else rApp down or slow
        R1-->>RM: 502 UPSTREAM_UNAVAILABLE or 504 → false
    end
```

## 01d — The rApp bootstraps as the instance

The rApp's own code calls nothing about identity: `smo_sdk.instances.install(app)` makes every request under `/instances/{id}/…` act for that
instance, so the SDK picks its token. The sample rApps bootstrap in `POST /instances/{id}/start` (the demo's **1 · Prepare and start**).

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    participant App as rApp code
    participant SDK as rApp SDK (R1Client)
    participant R1 as R1 Termination
    participant SME as SME
    participant RM as rApp Management
    participant DME as DME
    participant OAM as RAN NF OAM

    Op->>App: POST /instances/{id}/start<br/>(demo: 1 · Prepare and start → demo/prepare)
    Note over App: middleware: this request acts for instance {id}
    App->>SDK: rapp-mgmt GET /instances/{id} · DME get_dataset × 3
    Note over SDK: instance {id} bound? → its identity, else the process's own
    SDK->>R1: GET /bootstrap (once per process)
    R1-->>SDK: SME token endpoint
    SDK->>SME: POST /oauth2/token<br/>{client_credentials, client_id: oauthClientId, secret, scope smo-rapp}
    alt credentials valid
        SME-->>SDK: {access_token, expires_in}
    else secret renewed or invoker gone
        SME-->>SDK: 400 → no token (never replaced by a new identity)
    end
    SDK->>R1: GET /rapp-mgmt/instances/{id}, DME data jobs (Bearer token)
    Note over R1: introspect at SME → role rapp, invoker = oauthClientId<br/>role policy (INTERNAL_ONLY, RAPP_MAY_CHANGE), kill switch
    R1->>DME: data jobs TRAINING / INFERENCE / EMULATION (consumer = instance)
    DME-->>App: datasets
    Note over App: instance stored (cells, actuator, autonomy mode)
    App->>SDK: instances.bootstrap(sdk, id) → platform.bootstrap_complete(id)
    SDK->>R1: POST /rapp-mgmt/instances/{id}/bootstrap-complete
    R1->>RM: X-R1-Role: rapp, X-R1-Invoker-Id: oauthClientId
    Note over RM: rApp caller must be this instance (else 403 NOT_THIS_INSTANCE)<br/>state must be DEPLOYING (else 409)
    RM->>OAM: PUT /rapp-limits/{oauthClientId} (manifest limits)
    RM->>OAM: PUT /rapp-approval-policy/{oauthClientId} (if one was given)
    alt both put in force (or none asked)
        RM->>SME: POST provider and service registrations<br/>(package Files/Sme declarations, apfId = oauthClientId)
        Note over RM: best effort, unless RAPP_MGMT_REQUIRE_SME_REGISTRATION<br/>(on in values-production.yaml): then a refusal is<br/>502 UPSTREAM_FAILED, no answer 503, and it stays DEPLOYING
        Note over RM: DEPLOYING → RUNNING
        RM-->>App: 200 {state RUNNING}
        App-->>Op: 200 {…instance, bootstrap: {state RUNNING}}
    else RAN NF OAM unreachable or refused (fail closed)
        RM-->>App: 503 ENDPOINT_UNREACHABLE (stays DEPLOYING, may retry)
        App-->>Op: 200 {…instance, bootstrap: {state null, reason 503 …}}
    else not this instance's credentials / already RUNNING
        RM-->>App: 403 NOT_THIS_INSTANCE / 409
        App-->>Op: 200 {…instance, bootstrap: {state null, reason}}
    end
    Note over App: no credentials held → no call, reason NO_CREDENTIALS
```

From here on, every call the rApp makes for the instance carries the instance's identity: the instance's scope claim narrows what it reads and
writes, its limits and approval policy apply to its writes, the kill switch stops it, and the audit trail names it.

## 01e — Re-send the credentials

The rApp keeps an instance's credentials in the shared identity table, so a restart or another replica does not lose them. They are re-sent
when the rApp was down or unreachable when the instance was created (`credentialsDelivered: false`; the instance stays DEPLOYING and its
bootstrap says `NO_CREDENTIALS`), or to rotate the secret. The operator presses **Re-send credentials** on the rApp's page (admin).

```mermaid
sequenceDiagram
    actor Op as Operator (GUI, admin)
    participant RM as rApp Management
    participant SME as SME
    participant App as rApp

    Op->>RM: POST /rapp-mgmt/instances/{id}/credentials/renew
    Note over RM: instance exists (else 404), not terminated (else 409)
    RM->>SME: POST /sme/invoker-registrations/{oauthClientId}/secret (internal-only)
    Note over SME: new secret, same invoker id<br/>old secret refused, its tokens revoked
    alt new secret issued
        SME-->>RM: 200 {apiInvokerId, onboardingSecret}
    else SME unreachable or unknown invoker
        RM-->>Op: 503 ENDPOINT_UNREACHABLE
    end
    RM->>App: push as in 01c
    alt rApp took it
        App-->>RM: 204
        RM-->>Op: 200 {credentialsDelivered true}
    else rApp refused or unreachable
        RM-->>Op: 503 (the secret is not shown, retry)
    else instance has no operatorApiBase
        RM-->>Op: 200 {credentialsDelivered false, oauthClientSecret} (once, to hand over)
    end
```

The same renewal can run by itself (`SEC-15.19`). With `RAPP_MGMT_SECRET_ROTATION_DAYS` above 0 (default 0: off) the rApp Management worker renews the secret
of every RUNNING instance with an operator API whose secret is older than that, a few per run, oldest first. It first checks that the rApp answers through the
gateway and, if it does not, changes nothing at SME and tries again at the next run; it then asks SME for the new secret and pushes it, and writes an audit row
(actor `system:secret-rotation`) and an event (`org.oran.smo.rapp-instance.credentials-rotated`, or `...credentials-rotation-failed`) that hold no secret. SME keeps
one secret per identity, so there is no overlap: the old secret stops working when SME issues the new one, and the short window until the push lands is the cost
of a rotation. If the push fails, the old secret is already dead; the failure is recorded and the next run rotates again, or the operator presses Re-send now.

For a DEPLOYING instance `POST /rapp-mgmt/instances/{id}/credentials` makes a **new** identity instead (and pushes it); it is refused once the
instance runs, because its registrations at SME and DME are under the old id.

## Key decisions this flow depends on

- **One identity per instance.** rApp Management registers it at CreateInstance with the instance's scope claim; `oauthClientId` is what R1
  Termination vouches for and what the limits, kill switch, approval policy and audit trail key on (PR-SEC-14, PR-SEC-10.3, AI-10, AI-11).
- **The rApp acts as the instance it serves.** One rApp process may serve several instances, so the SDK holds one identity per instance and picks
  it per request (`smo_sdk.instances`, `smo_shared.r1_client.bind_instance_identity`); a call outside any instance uses the process's own.
- **Credentials are pushed, never fetched or shown.** rApp Management pushes them through R1 Termination to the instance's operator API, which
  accepts them from an SMO module only and keeps them in the shared identity table (every replica, and after a restart); rApp Management never
  stores the secret, and it reaches a browser only when there is no operator API to push to.
- **The rApp reports itself up.** `bootstrap-complete` from an rApp is accepted for its own instance only, and is where the instance's safeguards
  are put in force, fail-closed, before it may run.
- **NFO resolves a placement through FOCOM** even though Phase 1's answer is one cluster and nothing is started (NFO+FOCOM LLD section 4).
- **`autonomyMode` and `regionScope` are fixed at CreateInstance** for the instance's lifetime; call flow 09 shows what they drive (HISTORY.md
  OI-6.3). `usage/start` (first, at CreateInstance, which fails without it) and `usage/stop` (at terminate) let Onboarding's deprime and delete
  guards see every instance (call flow 06).
- **Decided in the review of this flow with the product owner (DOC-CF-1.1 review):**
  - A package can be uploaded from the console (`POST /onboarding/packages/upload`); the URL path stays for automation.
  - The package says where its rApp's operator API is (`operatorApi.baseUrl`), so Deploy needs no typed address; a typed one overrides it.
  - `SHADOW` stays the default autonomy mode: a new instance writes nothing until someone raises it.
  - Production (`deploy/helm/smo/values-production.yaml`) requires signed packages (the chart refuses to render without a trust store) and makes the
    SME registration of a package's declarations a condition of the bootstrap. The defaults keep both off, for a lab.
  - The instance secrets in the identity table (as the modules' own identities) are encrypted at rest when `SMO_IDENTITY_KEY` is configured (`SEC-15.18`: AES-256-GCM,
    one key for every replica, `python -m smo_shared.module_identity encrypt-existing` for the rows that predate it); without a key they are stored as before.
  - Secrets can be rotated on a schedule (`SEC-15.19`): `RAPP_MGMT_SECRET_ROTATION_DAYS`, default 0 (off), run by the `rapp-mgmt-worker`; an instance whose rApp cannot be
    reached is not rotated and is tried again at the next run (01e). Re-send credentials by hand stays.
