# Call Flow 21: O1 vendor onboarding — elements, schemas, the vendor's capability, and the two checks on every O1 operation

RAN NF OAM's vendor capability registry (`ran-nf-oam/app/vendors.py`; "O1 vendor onboarding" in `docs/ARCHITECTURE.md`, Wave 9 in `docs/STANDARDS.md`)
makes onboarding a RAN vendor (or a Digital Twin) data, not code: an element is registered behind its O1 adaptor, the vendor's data-model descriptor is
loaded, and its capability is declared (by hand, or discovered from a registered adaptor). Two generic checks then read the registry on every O1 operation:
the **axis-2 presence guard** (does the element's vendor implement this MnS service) and the **axis-3 schema check** (do the class, attribute and value exist in
the data model the vendor's conformance mode selects). Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 21a | Registering an element | Operator (GUI), an SMO module | an element behind its adaptor, inside the caller's scope, its services no wider than its vendor's |
| 21b | Schemas and the vendor's capability | Admin (GUI), an SMO module | a checked descriptor loaded, a capability declared, each change published |
| 21c | Onboarding from a registered adaptor | Admin (GUI) | the adaptor's declaration read from its registered origin, checked, schemas loaded, the capability declared in one commit |
| 21d | The two checks on a CM write | an rApp through DME, a module, Operator (GUI) | a write refused before anything is sent, or dispatched |

**As images:** [21a](images/21-o1-vendor-onboarding/21a-registering-an-element.png) ·
[21b](images/21-o1-vendor-onboarding/21b-schemas-and-the-vendor-s-capability.png) ·
[21c](images/21-o1-vendor-onboarding/21c-onboarding-from-a-registered-adaptor.png) ·
[21d](images/21-o1-vendor-onboarding/21d-the-two-checks-on-a-cm-write.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach RAN NF OAM through R1 Termination, which stamps the
caller's role and scope claim; an rApp may only read the registry (R1's allow-list, and each write route refuses the rApp role itself). Registry changes are
published on the event feed (`org.oran.smo.vendor-capability.*`, `org.oran.smo.cm-schema.loaded`, `docs/EVENTS.md`).

## 21a — Registering an element

```mermaid
sequenceDiagram
    participant C as Operator (GUI) / SMO module
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    C->>OAM: POST /o1-adaptor-endpoints {managedElementRef, adaptorUri, transport, o1Protocol, vendorName?, supportedServices?, region?, tenant?, credentialRef?}
    alt an rApp
        OAM-->>C: 403 ROLE_NOT_PERMITTED
    else region or tenant outside the caller's scope claim (or none given by a scoped caller)
        OAM-->>C: 403 SCOPE_DENIED
    else http-mock adaptorUri not an http(s) URL with a host and no credentials, ssh or tls URI that does not parse
        OAM-->>C: 422
    else o1Protocol not a mode the vendor declared
        OAM-->>C: 409 PROTOCOL_NOT_SUPPORTED
    else an adaptor already registered for the element
        OAM-->>C: 409 SERVICE_NAME_CONFLICT
    else supportedServices wider than the vendor's declared ones
        OAM-->>C: 422
    else
        OAM->>DB: endpoint (DISCOVERED), managed entity with its place, containment tree, onboarding (if a template matches), one commit
        OAM-->>C: 201 {endpointId, managedElementRef, healthStatus, region, tenant}
    end
    Note over OAM: the services an element is offered are its endpoint's narrowed to its vendor's, read at every operation,<br/>so an endpoint registered before its vendor's entry, or a vendor narrowed later, cannot widen them
```

The element used to be registerable by anyone who reached the route, anywhere (no scope check), at any URL, and its own service list won over its
vendor's for ever. **Phase 1:** there is no route to deregister or move an endpoint; a re-registration is 409.

## 21b — Schemas and the vendor's capability

```mermaid
sequenceDiagram
    participant A as Admin (GUI) / SMO module
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    A->>OAM: POST /cm-schemas {schemaName, revision, type, location, descriptor {classes {IOC {attribute entry}}}}
    alt an rApp
        OAM-->>A: 403 ROLE_NOT_PERMITTED
    else an entry the leaf checker cannot use (not an object, unknown type, malformed enum, range, length, pattern or fractionDigits)
        OAM-->>A: 422 SCHEMA_VALIDATION_FAILED naming IOC.attribute
    else name and revision already loaded or bundled
        OAM-->>A: 409 CM_SCHEMA_CONFLICT (schemas are immutable)
    else
        OAM->>DB: schema, event cm-schema.loaded, one commit
        OAM-->>A: 201 the schema
    end
    A->>OAM: PUT /vendor-capabilities/{vendor} {supportedServices, conformanceMode SPEC|OWN|COMBINED, supportedVendorModes, schemaRef?, specSchemaRef?}
    alt OWN or COMBINED without schemaRef / a referenced schema not loaded or bundled
        OAM-->>A: 422 / 404 CM_SCHEMA_NOT_FOUND
    else a registered element of the vendor uses a mode the declaration drops
        OAM-->>A: 409 PROTOCOL_NOT_SUPPORTED, nothing kept
    else
        Note over OAM: SPEC and COMBINED default specSchemaRef to the bundled 3gpp-ts28541-nrnrm@19.6.0
        OAM->>DB: capability, event vendor-capability.declared, one commit
        OAM-->>A: 200 the entry, with unresolvedSchemas
    end
    A->>OAM: DELETE /vendor-capabilities/{vendor}?force
    alt elements use the vendor and no force
        OAM-->>A: 409 VENDOR_CAPABILITY_IN_USE (n elements would be left unchecked)
    else
        OAM->>DB: entry deleted, event vendor-capability.deleted {elementsLeftUnchecked}, one commit
        OAM-->>A: 204 (also for an unknown vendor, or one outside a scoped caller's elements)
    end
```

A malformed descriptor entry used to load and then make every later CM write touching it a 500, with no way to replace the schema; an rApp could load a
schema under a name an admin would need (`POST /cm-schemas` had no role check, only R1's allow-list); deleting a used entry silently switched off both checks
for every tenant's elements of the vendor; and no registry change was announced.

## 21c — Onboarding from a registered adaptor

```mermaid
sequenceDiagram
    participant A as Admin (GUI)
    participant OAM as RAN NF OAM
    participant AD as The vendor's O1 adaptor
    participant DB as RAN NF OAM database

    A->>OAM: POST /vendor-onboarding {vendorName, discoverFrom = a registered managedElementRef, conformanceMode, schemas[], schemaRef?, supportedServices?}
    alt an rApp / the element unknown / registered for another vendor / reached over ssh or tls
        OAM-->>A: 403 / 404 / 422 / 409 PROTOCOL_NOT_SUPPORTED
    else
        Note over OAM: the URL is the element's registered adaptorUri origin + /capabilities, never one from the request,<br/>fetched through the SSRF guard (smo_shared.webhook)
        OAM->>AD: GET /capabilities
        alt no answer, or not 200
            OAM-->>A: 503 ENDPOINT_UNREACHABLE
        else not a JSON object, lists that are not lists, an unknown service or mode
            OAM-->>A: 502 UPSTREAM_FAILED, nothing declared
        else the adaptor names another vendor
            OAM-->>A: 422
        else
            Note over OAM: body values win over discovered ones, the listed schemas are loaded (21b checks,<br/>an identical one reused), OWN or COMBINED with one schema takes it as schemaRef
            OAM->>DB: schemas, capability, events, one commit (a failure at any step keeps nothing)
            OAM-->>A: 201 {discovered, schemasLoaded, capability}
        end
    end
```

An adaptor answer that was not a JSON object, or named an unknown service or mode, was a 500. An adaptor that omits `vendorName` is taken as the named
vendor's.

## 21d — The two checks on a CM write

```mermaid
sequenceDiagram
    participant R as rApp (through DME) / module / Operator (GUI)
    participant OAM as RAN NF OAM
    participant AD as The element's adaptor

    R->>OAM: POST /config-jobs {changes [{managedElementRef, className? or managedFunctionRef IOC=id, attributeChanges}]}
    Note over OAM: before any job exists, for every change
    alt axis 2: the element's services (endpoint narrowed to vendor) lack PROV
        OAM-->>R: 409 O1_SERVICE_NOT_SUPPORTED, nothing sent
    else axis 3: a schema the vendor names is gone
        OAM-->>R: 422 SCHEMA_VALIDATION_FAILED (the schema named), nothing sent
    else axis 3: class, attribute, type, range, length, pattern or enum not in the selected model, or a stored entry the checker cannot use
        OAM-->>R: 422 SCHEMA_VALIDATION_FAILED, every offending attribute named, nothing sent
    else no capability entry for the vendor
        Note over OAM: unchecked (the permissive default), then as valid
        OAM->>AD: edit-config RPC (call flow 03)
    else valid
        OAM->>AD: edit-config RPC (call flow 03)
        OAM-->>R: 202 the job
    end
    Note over OAM: the same presence guard refuses FM (alarm ingest, FM subscriptions, call flow 20), PM subscriptions,<br/>and SWM jobs (call flow 19) for an element without that service
```

A vendor entry whose schema was missing (a loaded one removed, a bundled one dropped by a later release) was quietly unchecked under SPEC, and refused every
class under COMBINED; it now refuses with the schema named, and `GET /vendor-capabilities` lists it in `unresolvedSchemas`.

## Decisions this flow depends on

- **What conformance mode means**: `SPEC` the spec descriptor only (default `3gpp-ts28541-nrnrm@19.6.0`, generated from `specs/5G_APIs/TS28541_NrNrm.yaml`),
  `OWN` the vendor's only, `COMBINED` the spec's with the vendor's named augments; values are checked against the entry's type and constraints
  (`leafcheck.py`).
- **How a change's class is found**: `className`, else the last RDN of `managedFunctionRef`; a change naming no class must name attributes some class defines.
- **The registry is per vendor; an endpoint may only narrow it** (DOC-CF-1.8): the services in force are the endpoint's intersected with the vendor's, at
  every operation, not only at registration.
- **No registry, no check**: an element whose vendor has no entry skips both checks, so a deployment that registers none works unchanged; deleting a used
  entry therefore needs `force` (DOC-CF-1.8).
- **A schema the registry names is there, or writes stop** (DOC-CF-1.8): fail closed with the schema named rather than unchecked.
- **Descriptors are checked when loaded** (DOC-CF-1.8), because a loaded schema is immutable.
- **Only the operator or a module changes the registry or registers an element** (SEC-15.7b, DOC-CF-1.8), inside its scope claim.
- **Discovery never fetches a caller-chosen URL**, and its answer is checked like any input (DOC-CF-1.8).
- **A refused write never reaches the adaptor**: the pre-check runs before a `WriteConfigJob` exists, and DME passes the 4xx back to the rApp.
- **Not taken:** deregistering or moving an endpoint, vetting the adaptor address for CM dispatch beyond its scheme and host (the registrant is the
  operator or a module), a YANG front end for the ingestion script, and transports beyond NETCONF (HTTP bridge, SSH, TLS) and RESTCONF.
