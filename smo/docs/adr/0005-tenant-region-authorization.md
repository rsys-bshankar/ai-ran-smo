# ADR 0005 — Tenant and region authorization: where scope is enforced

Status: accepted (`PR-SEC-10`, steps `SEC-10.1` to `10.6`, amended by `SEC-10.9`, `10.11` and the part of `10.7` that the data allows, see 10; `10.7` for the rest, `10.8`, `10.10` and `10.12` are open, see 9). Date: 2026-10-09. Release 0.7.0.

## Context

Before this record an rApp with a valid token could read and change every managed element of the network. The role policy of `PR-SEC-14` says *what kind* of call an rApp may
make (`smo_shared/roles.py`); nothing said *on which targets*. A deployment that hands rApps to more than one party, or that runs one SMO for more than one tenant or region, needs the
second answer: this rApp may touch the elements of region `eu-west` and tenant `acme`, and no others.

Decided with the owner: both axes (region and tenant) are in scope; the first enforcement is `POST /config-jobs` (the pilot), then configuration reads, alarms and PM, then DME and
MLMR; an OPA sidecar as an alternative decision point (`SEC-10.8`) is not part of this record.

Two things are needed for every decision: **who is asking** (a scope claim) and **what is asked for** (the target's region and tenant). R1 Termination sees the first but not the second:
it sees a path and a body, and the target is in a field of a body (`changes[].managedElementRef`), in a path (`/managed-entities/{ref}/config`), behind an id the system
minted (`/alarms/{id}/ack`, `/config-jobs/{id}/rollback`) or in a stored request that is replayed later (an approved action). The module that owns the data knows the target.

## Decision

### 1. Who decides: the module that owns the target; the gateway vouches for the claim

**Option A, enforce at R1 Termination.** One place, nothing for a module to forget. Rejected: the gateway would have to understand every module's request shapes to find the target, and to
look the target up (a registry read for each call, a second source of truth beside the module's own), and it cannot do the parts that are not a yes or a no on the request: filter a list,
hide an item behind an id, re-check an approval when it is approved weeks after it was asked, or follow a call an SMO module makes for an rApp (DME writes for the rApp; at the gateway that call
is DME's). It would also put every module's data model in the gateway, which carries no domain schema (SMO Design v1.3 section 3.3).

**Option B, enforce in each module that owns the target, with one shared helper.** The decision is where the data is. Cost: every module that exposes scoped data must call the helper, and
one that forgets is open. Mitigated by one rule written once (`smo_shared/scope.py`: `permits`, `filter_statement`, `denied_condition`, `covers`), one adapter per module
(`ran-nf-oam/app/scoping.py`), and tests that walk the refusals per route.

**Decided: B, with the gateway as the voucher of the claim**, exactly as it vouches for the invoker id and the role:

- SME holds the claim (`invoker_registration.authz_scope`) and returns it in the introspection as `authz_scope`, read live;
- R1 Termination forwards it in `X-R1-Scope` (compact JSON) and drops any value of that header, and of `X-R1-On-Behalf-Scope`, that a caller sent, before it sets its own. A backend
  therefore reads only the gateway's claim. The test that proves it is `r1-termination/tests/test_scope.py` (a spoofed header is dropped for an unscoped caller and overwritten for a scoped one);
- an SMO module acting for an rApp (`X-R1-On-Behalf-Of`) passes the rApp's claim on in `X-R1-On-Behalf-Scope`, which `R1Client` adds by itself beside the id; the gateway forwards it only from an
  `internal` caller, as it does the id. A write through DME is held to the rApp's scope at RAN NF OAM (`tests_integration/test_tenant_region_scope.py`);
- the module reads the claim with `scope_of(request.headers)` and applies `permits` to the target's `region` and `tenant`.

The seam for `SEC-10.8` is the one place a module calls: replacing `permits` with a call to a policy engine changes the helper, not the routes.

The introspection cache (`PR-SEC-5.4`) keeps the claim with the role: a change takes effect after at most `R1_INTROSPECTION_CACHE_SECONDS`, at once on the replica that carried the change
(`PUT /sme/invoker-registrations/{id}/authz-scope` evicts the invoker's entries).

### 2. The claim

`authzScope` (registration, API) / `authz_scope` (introspection) / `X-R1-Scope` (header): a JSON object with two optional keys.

```json
{"regions": ["eu-west", "eu-north"], "tenants": ["acme"]}
```

- Each key is a list of 1 to 100 distinct values; a value is 1 to 100 characters of letters, digits and `. _ : / @ + -`, starting with a letter or digit. An empty list is **not** a valid claim
  (a caller that may touch nothing is an rApp that should be stopped: the kill switch); `{}` and absent are the same: **no claim**. Anything else is refused with 422 `AUTHZ_SCOPE_INVALID`
  and a fixed message.
- Matching is exact and case-sensitive. There are no wildcards and no hierarchy (a region does not contain sub-regions). Not taken: both would need a model of the geography/tenant tree that
  nobody has yet; a list of exact values covers "these three regions" today, and a hierarchy can be added as a new key without breaking a claim.
- The name is `authzScope`, not `scope`: SME already has an OAuth `scope` (the API names a token is for, **chosen by the caller** in its token request, so it cannot carry a restriction), ProvMnS has
  a `ScopeType`, and an rApp instance has `regionScope` (where an AUTONOMOUS instance's intents go).

The claim is a property of the **invoker**, not of the token and not of the package. It is set when the invoker is registered at SME or later by an operator; it is not a key of
`manifest.yaml`: reach is granted by the operator who creates the instance, not claimed by the author of the package (`docs/RAPP_PACKAGING.md`). rApp Management takes it at
`POST /instances` (`authzScope`), puts it on the instance's invoker when it registers it (and again on a new credential), keeps it through an upgrade and restores it on a rollback.
If SME does not echo the claim it recorded (an SME of the previous release ignores the field), rApp Management takes the new invoker away again and fails the request (503): never an unscoped identity
where a scoped one was asked for.

**A scoped caller cannot widen itself.** `POST /sme/invoker-registrations` is open to an rApp (its SDK registers). A scoped caller that registered a fresh invoker would leave its scope.
So the new invoker carries the caller's claim, or a narrower one it names (`covers`); a wider one is 403 `SCOPE_DENIED`. Editing a claim (`PUT .../authz-scope`) is internal-only at the gateway
and absent from the rApp change allow-list.

### 3. The target

`managed_entity.region` and `managed_entity.tenant` (revision `0032`, nullable, indexed), set when the element is registered (`POST /o1-adaptor-endpoints`: `region`, `tenant`) and edited by
an operator (`PUT /ran-nf-oam/managed-entities/{ref}/scope`; the GUI backend allows it to an admin). Same alphabet as the claim. There is no discovery path that creates elements; an
element is registered, and so given its place, by whoever registers it.

### 4. Semantics (the table)

| Caller | Target `region` / `tenant` | Result |
|---|---|---|
| **unscoped** (no claim): an SMO module on its own account, the operator's GUI, an rApp nobody scoped | anything, set or not | **permitted**. Nothing is asked, no query is made. This is the whole of what an upgrade changes: nothing, until a claim is set |
| claim names `regions` only | region is one of them (tenant: any, set or not) | permitted |
| claim names `regions` only | region is another, or **not set** | **refused** |
| claim names `tenants` only | tenant is one of them (region: any, set or not) | permitted |
| claim names `tenants` only | tenant is another, or **not set** | **refused** |
| claim names both | both match | permitted |
| claim names both | either differs or is not set | **refused** |
| any claim | the element **is not registered** | **refused** (it has no region and no tenant) |
| a claim that cannot be read (damaged header, a store that holds an invalid value) | anything | **refused**: `DENY_ALL`; a restriction is never dropped because it was damaged |

Fail closed for a scoped caller meeting an unscoped target: a target with no region/tenant is visible only to unscoped callers. So a deployment that scopes its first rApp must first give its
elements a region and a tenant; until then that rApp sees nothing, which is the safe direction.

### 5. What a refusal looks like (and avoids telling)

| The request | A target outside the scope | Why |
|---|---|---|
| a write that names targets (`POST /config-jobs`, also `dryRun`; `POST /config-jobs/{id}/rollback`; subscribing to PM/FM for an element) | **403 `SCOPE_DENIED`** for the whole request: one element outside refuses all of it, nothing is checked, recorded or sent first | the caller named the targets; refusing is the honest answer |
| a read that names an element by reference (`GET /managed-entities/{ref}`, `.../config`, `.../config-history`, `.../diff`) | **403 `SCOPE_DENIED`** | likewise |
| an item addressed by an id the system minted (`GET /config-jobs/{id}`, `PATCH /alarms/{id}/ack|clear`, `GET /pm-files/{id}/file`) | **404** (`CONFIG_JOB_NOT_FOUND`, `ALARM_NOT_FOUND`, `NRM_OBJECT_NOT_FOUND`) as if it did not exist | the id was not the caller's to know; a 403 would confirm it exists |
| a list (alarms, PM and FM subscriptions, files, jobs, managed elements, endpoints, cell guards, software jobs; a KPI is computed over the permitted elements only) | **filtered** to what the caller may touch; `total` counts what it may see; never refused | a page of someone else's rows is the leak; a refused list would make an empty scope an error |
| `DELETE` of a subscription by id | **204**, nothing removed | idempotent delete already answers 204 for an id that is not there |
| a managed object addressed by its DN (`GET /managed-objects/{dn}` and below, `GET /topology/relation`) | **404** `MANAGED_OBJECT_NOT_FOUND`, the same answer as for a DN that is not in the tree (section 10) | a DN names an object, not an element: a 403 beside a 404 would tell the caller which DNs exist |

To avoid telling a scoped caller which references exist, an element that is not registered is refused exactly as one that is outside the scope (same status, same title, same shape of detail), and
the order of checks puts the scope first (before the schema check, which would answer 422 for an attribute of an element the caller may not know). The detail of a refusal names the references
the caller itself sent, never others: a rollback, whose elements the caller did not send, names none.

A refusal of a **known caller** is recorded like the other safeguard refusals (`safeguard_refusal`, code `SCOPE_DENIED`, `GET /safeguard-refusals`, a `RAPP_SAFEGUARD_REFUSAL` event to
`safeguard-subscriptions`), so an operator sees an rApp that reaches outside its scope. A refused read is not recorded (reads are many, and R1 audits only changes).

### 6. Approvals (`PR-AI-11`)

A request is checked when it is made (a request outside the scope is refused at once and never parked). The requester's claim is kept with it (`rapp_action_approval.requester_scope`) and
**checked again at approval, against the targets as they are then**: an element moved to another region or tenant while the request waited refuses the approval, the request is closed `REFUSED` with
`refusalCode` `SCOPE_DENIED`, and nothing is written. The approver is an operator (an `internal` caller, unscoped): approving never widens what the rApp may touch. Chosen: a **snapshot**
of the claim, not a lookup of the rApp's current claim at approval, because RAN NF OAM does not hold claims (SME does) and a synchronous call to SME in the approve path would add a dependency to a
decision that must not depend on it. Consequence: narrowing a claim does not reach a request already parked; rejecting it, or stopping the rApp (the kill switch is checked again at
approval), does. Scoping the human approvers themselves (a GUI user who may decide only for one tenant) is the console user's claim (section 11): the decision is
narrowed by it like every other call of that person.

### 7. Rollback, revert and the staged job

A rollback writes to every element the job wrote to, so every one of them must be inside the caller's scope (dry run included); otherwise 403 with a detail that names none. The automatic
revert of a failed wave (`MGT-5.5`) and the KPI-guard revert run in the module on the job's own record, not as a caller's request, and are not scoped (undoing must never be refused). The
wave actions (`continue`, `halt`, `abort`) and the KPI check are not on the rApp change allow-list.

### 8. The operator

The operator's GUI backend is an `internal` caller and unscoped; a person using it may carry a claim of their own (section 11). `SEC-10` limits rApps and the SMO
modules acting for them.

### 9. What is not in this record

- **`SEC-10.7`, DME and MLMR reads (the rest of it; the action list is section 10).** DME's data (types, producers, jobs, records) and MLMR's models carry no managed element, region or tenant: there is nothing for the helper to match on, and
  inventing the attribute (a tenant on a model, on a data type) is a data-model decision of its own. The one DME route that names an element, `POST /dme/actions`, forwards the write to RAN
  NF OAM with the rApp's claim and is therefore scoped there; `GET /dme/actions` lists a record with an element reference and was not filtered in the first step (section 10 filters it). The rest is left open in `OPEN_ITEMS.md` with these reasons.
- Reads in RAN NF OAM were not scoped in the first step: the managed-object tree and topology, the KPI schedules, the file subscriptions, the vendor and CM-schema registries. Section 10 scopes them. (KPI computation, `GET /kpis/{name}`, was: it reads only the performance files of the elements inside the scope, so `all` is the caller's elements, not the network's.)
- Ownership of a job (an rApp rolling back another rApp's job inside its scope) was left out of the first step; section 10 adds it.
- Database row-level security as a second line, and an OPA sidecar (`SEC-10.8`): both would sit behind the same seam.

### 10. Amendment: the rest of RAN NF OAM's reads, ownership of a job, and the DME action list

Decided with the owner after the first step shipped; the same rule (`smo_shared/scope.py` is unchanged), the same status codes, applied to what was left. Tests: `ran-nf-oam/tests/test_scope_reads.py`, `dme/tests/test_scope.py`, `tests_integration/test_tenant_region_scope.py`.

- **The managed-object tree and the topology (`SEC-10.9`).** A node carries `managed_element_ref`, so the rule applies row by row. By DN: **404**, identical to a DN that is not in the tree (a DN is an object name; the scope-first order of section 5 would otherwise answer 403 for a hidden object and 404 for a missing one). `children` and `subtree` leave out nodes of other elements; `GET /topology` is the export of the caller's elements. `GET /topology/links` is worked out **among the caller's elements** (the query of elements is restricted before the links are computed), so a neighbour declared on an element outside is `EXTERNAL` for it, as if no element declared it, and no answer names an element it may not touch; `AMBIGUOUS` and `reciprocal` are worked out inside the same world. The walk of an element (`POST .../managed-objects/refresh`) and the cell-guard writes name an element: **403**.
- **KPI schedules (`SEC-10.9`).** A schedule names an element or none. One that names an element inside the scope is shown (list, by id) and may be replaced or removed; one that names none is the whole network's and is for an unscoped caller; by id **404**; `PUT` of an element outside, of no element, or over an id that exists and is hidden is **403** (the id does not leak beyond that). The worker that runs schedules is not a caller and is not scoped.
- **File subscriptions (`SEC-10.9`).** A subscription is sent the `notifyFileReady` of every file of every element, so no part of it lies inside a claim that restricts the network: creating one is **403**, a delete is **204** and removes nothing (a scoped caller never has one). Not taken: remembering the subscriber's claim with the subscription and sending it only the files inside it (a column, a migration, and a delivery path that has to look the claim up per file); an rApp cannot create a file subscription through the gateway today (`RAPP_MAY_CHANGE`), so the refusal is the module's own defence in depth.
- **The registries (`SEC-10.9`).** `vendor_capability` and `cm_schema_cache` have no element, region or tenant. **Decided: derived, not invented.** A scoped caller sees a vendor's capability entry when one of the elements inside its scope has that `vendorName` (404 otherwise, as if there were none; `GET /capabilities` is over those vendors), and a loaded CM schema when one of those entries names it as its own or its spec schema. The schemas bundled with the service (3GPP, O-RAN) are the same everywhere and about no element, and are shown to everyone. This reveals nothing about the rest of the network (which vendors it runs, the vendors' own models, a discovery address) and keeps what an rApp needs to read the checks that apply to its own elements. Not taken: a region and tenant on the registries (a data-model decision of the kind `SEC-10.7` waits for), or hiding the registries from every scoped caller (an rApp that reads its elements' schemas would break).
- **Ownership of a job (`SEC-10.11`).** A job belongs to the invoker it was made under (`write_config_job.invoker_id`, already stored for the rate limit); a rollback or revert of it, made on the record of the job and carrying no invoker of its own, belongs to whoever owns the job it undoes, however many rollbacks deep (a recursive query in lists, a walk by id). A job an operator or an SMO module made belongs to no rApp. **Who is held to it: an rApp that carries a scope claim**, and an SMO module acting for one (`X-R1-On-Behalf-Of`). An SMO module on its own account (the GUI, an admin's tool) and an rApp with no claim are not, so an upgrade changes nothing until a claim is set, as for the scope itself. The owner can widen it to every rApp by dropping the claim test in `_job_owner_filter` (`ran-nf-oam/app/main.py`): that would change what an unscoped rApp may read, which the owner asked this step not to do. **What it covers:** `GET /config-jobs/{id}`, the list, `POST /config-jobs/{id}/rollback` (all **404**, asked before the scope, so the answer does not say whether the job exists), and, found while tracing the same gap, `GET /rapp-approvals/{id}` and `GET /decision-records/{id}` (404 for another rApp's; the lists are internal-only at the gateway). **"Its instance":** an instance has one invoker (`oauthClientId`), so the instance's jobs are the invoker's. A second invoker that an rApp registered for itself (`POST /sme/invoker-registrations`) is another owner, because RAN NF OAM cannot know the relationship without asking rApp Management. A refused rollback is not recorded as a safeguard refusal (that would need a new code in the safeguard vocabulary).
- **The DME action list (`SEC-10.7`, the part the data allows).** `dme_action_record` names elements (the first change's `managed_element_ref` and every change's own). DME owns no region or tenant, so it **asks RAN NF OAM**: `GET /ran-nf-oam/managed-entities` through `R1Client`, which passes the rApp's claim on, returns exactly the elements inside it; an action is shown when it names at least one element and all it names are among them (one outside hides it, as for a job). `GET /dme/actions` is filtered and `total` counts what is shown; `GET /dme/actions/{id}` is a 404. Fail closed: if the claim could not be passed on (no invoker id), or RAN NF OAM does not answer, the caller is shown nothing (502 when it cannot say). An unscoped caller asks nobody. Cost: one pass through the element list (pages of 500) and a scan of the action table for each scoped list request. **Still not possible, and why:** DME's types, producers, offers, jobs and records, and MLMR's models and repositories carry no element, region or tenant (a record's payload may hold a `managedElementRef`, but only a producer's own schema says so, and DME does not read payloads). A scoped rApp therefore reads all of them. The fix is a data-model decision (an owner tenant on a data type or a model), not a change to the rule.
- **MSAC on the lists and the subscription deletes (`MGT-2.6`).** With `RAN_NF_OAM_MSAC_REACH` on, the model is the scope filter: a list route leaves out the rows whose element the caller (a registered MSAC Identity) may not `read` (the target of a row is its element, `/ManagedElement=X`); a job is shown only when every element it wrote to is readable; `GET /kpis/{name}` is computed over readable elements; the managed-object tree and topology are filtered the same way and a read of a node by DN needs `read` on its element (403 `MSAC_ACCESS_DENIED`, asked after the scope). A delete of a PM or FM subscription needs `read` on its element (the right that created it) and of a file subscription `read` on the whole network; otherwise 204 and nothing removed. Off by default as before; the registries, the KPI schedules, and the software-campaign and onboarding lists are not covered (no single target).

### 11. Amendment: a console user's claim (`GUI-5`)

- **Where the claim lives.** On the console user (`gui_user.scope` in the GUI backend's own store), with the same rules as an invoker's (two axes, 1 to 100 values each,
  the value alphabet, no duplicates). An admin sets it on the Users tab; with `GUI_OIDC_SCOPE_CLAIM` set, the identity provider's token gives it at every sign-in, as
  it gives the role, and a claim in the token that breaks the rules refuses the sign-in (a restriction is never dropped because it was damaged). No claim: unscoped,
  as every console user was before.
- **How it travels.** The backend is one `internal` invoker for every user, so the claim cannot be SME's: it goes beside the person the backend already names
  (`X-R1-Acting-User`, `SEC-15.8`) in `X-R1-Acting-User-Scope`. R1 Termination drops any inbound value and forwards it only from an `internal` caller and only
  with the acting user. `scope_of` reads it in that case and **narrows** the caller's own claim by it (per axis, the values both list), so a person can never
  widen what the console itself is allowed. It is not `X-R1-On-Behalf-Scope`: that header comes with `X-R1-On-Behalf-Of`, which makes the request the rApp's for
  ownership and the safeguards, and a person is not an rApp (they keep seeing every job, as an operator does).
- **What the backend computes for many users** (the summary counts, the event stream's pages, the typeahead, a background export) is asked with the claim and
  cached under it, so two users with different claims never share an answer; an export reads as the person who asked, with their claim as it is when it runs.
- **What it does not narrow**, as for a scoped rApp (section 9): the modules with nothing to match (rApp Management, AIMgF, MLMR, DME's types), and the GUI
  backend's own data (its audit log, users, exports list, preferences). An admin manages users and roles whatever their own claim.

## Consequences

- An upgrade changes nothing: the columns are nullable, no claim is set, and an unscoped caller is never asked. `0032` is expand-only.
- Putting an rApp in a scope is two operator steps: give the elements a region and a tenant, create the instance with an `authzScope`. The GUI shows both.
- Every new module route that exposes data about a managed element must call the helper; the route-by-route list is in `ran-nf-oam/README.md` and the tests are `ran-nf-oam/tests/test_scope.py`.
- The helper is mutation-tested (`shared/pyproject.toml`).
