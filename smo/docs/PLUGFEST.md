# Plugfest plan (STD-3)

Two parts: which O-RAN test specification applies to each interface this SMO implements (STD-3.1), and a one-page plan for a plugfest with a counterparty (STD-3.2). The plan has not been run: it needs a counterparty (a vendor's O1 adaptor or network function, an rApp, an O-Cloud) and is open in `OPEN_ITEMS.md`.

**No document number in this file is invented.** The interfaces and the specification families are named as O-RAN publishes them. Where the exact test specification, its number or its release could not be stated with confidence, the cell says "to be confirmed against the current O-RAN specification release" and the owner or the counterparty fills it in. The 3GPP releases this build is checked against are in `../../specs/README.md` (STD-2.1); this file does not repeat them.

## STD-3.1 Interface to test specification

| Interface | What this SMO implements | Counterparty at a plugfest | O-RAN interface specification | O-RAN test specification | Where this repo already checks it |
|---|---|---|---|---|---|
| R1 (rApp to SMO and Non-RT RIC framework) | R1 Termination (the gateway) in front of SME (service exposure, onboarding of invokers, access tokens), DME (data), rApp Management, Onboarding, AIMgF (AI/ML) and Intent Service; flat REST, OpenAPI per module (`docs/openapi/`) | An rApp from another vendor, or an SMO/Non-RT RIC that consumes the exposed services | WG2 R1 interface specifications (general aspects and principles, and the service-specific ones) | To be confirmed against the current O-RAN specification release | `tests_integration/` (contract, authorization walk, DAST), the four sample rApps, `docs/RAPP_PACKAGING.md` |
| O1, management services (CM, FM, PM, file management, software management, heartbeat) | RAN NF OAM as the O1 consumer, southbound through an O1 adaptor over NETCONF (SSH or TLS) and RESTCONF; the adaptor surface is `mock-o1-adaptor` (`docs/adr/0002-netconf-over-ssh.md`) | A managed element (O-DU, O-CU) or a vendor's O1 adaptor in front of one | WG10 O1 interface specification and the O1 information and data models (`specs/O-RAN-WG10-*`); the 3GPP management services it reuses (TS 28.532 and the others of STD-2.1) | To be confirmed against the current O-RAN specification release. WG10 and the Testing and Integration Focus Group (TIFG) are where to look | `conformance/o1` (the kit: checks in both directions, run against the mock on every pull request); `ran-nf-oam/tests/test_netconf_*` |
| O2 (O2ims, inventory and fault/performance of the O-Cloud) | FOCOM: the O2ims resources (inventory, fault, performance, artifacts, provisioning) at REST level, no real cluster behind a ProvisioningRequest | An O-Cloud IMS (a real or emulated one) | WG6 O2 interface specifications (O2ims, and O2dms for deployment management) | To be confirmed against the current O-RAN specification release | `focom/tests`, `docs/STANDARDS.md` (O2IMS row) |
| O2 (O2dms, deployment of workloads) | NFO: O-Cloud deployment orchestration (`docs/ARCHITECTURE.md`); `docs/STANDARDS.md` has no O2dms compliance row, so no conformance with O2dms is claimed | An O-Cloud DMS | WG6 O2dms specifications | To be confirmed against the current O-RAN specification release; do not plan to test it before the owner decides the scope | `nfo/tests` |
| A1 | **Not implemented.** Out of scope at every stage; the `a1-related` module was removed in 0.5.0 (`OPEN_ITEMS.md`, `CHANGELOG.md`) | none | WG2 A1 interface specifications | none, not planned | none |
| E2 and the Near-RT RIC | **Not implemented.** Out of scope at every stage | none | WG3 specifications | none, not planned | none |
| Open Fronthaul M-plane (O-RU) | **Not implemented** and not planned: the O-RU management-plane YANG modules are carried as reference only (`../../specs/README.md`) | none | WG4 specifications | none, not planned | none |
| rApp package and onboarding | A CSAR with a manifest (`docs/RAPP_PACKAGING.md`), which is this build's own layout | An rApp vendor | To be confirmed against the current O-RAN specification release (the R1 and rApp-lifecycle specifications of WG2) | To be confirmed against the current O-RAN specification release | `tests_integration/test_*_rapp.py`, `samples/` |

Two cautions. First, "implemented" here means at REST level against the specification files in `specs/`, with flat addressing instead of a DN containment tree (`docs/STANDARDS.md`); a counterparty that expects the tree will find the difference. Second, the O-RAN test specifications are written for a counterpart implementation (a vendor's SMO, a certified O-RU), and not every one has a role for an SMO reference stack. The row says "to be confirmed" until someone has read the current text and decided which test cases apply.

## STD-3.2 The plan

### Scope

Interoperability between this SMO and one counterparty at a time, on the interfaces the table lists as implemented: O1 (southbound, to the counterparty's adaptor or element), R1 (northbound, a counterparty's rApp using the SMO's services, or the SMO's rApps against a counterparty's R1), O2ims (FOCOM against a counterparty's O-Cloud IMS). Out of the plan: A1, E2, the Near-RT RIC, the Open Fronthaul, performance and scale tests, security testing of the counterparty.

### What the reference stack offers a counterparty

| Offer | What it is | How to use it |
|---|---|---|
| O1 conformance kit | `conformance/o1`, a command-line tool: checks an adaptor on discovery, NETCONF, RESTCONF and, with `--oam-url`, on what it emits (alarms, PM reports and files, software-update phases, heartbeats) | `python -m conformance.o1 --adaptor URL --oam-url URL`; the report is `report.json` and `report.md` (`conformance/README.md`) |
| rApp conformance pack | `conformance/rapp`: checks a rApp package against Onboarding's own validation (offline, with signatures checked against a trust store) and takes it through register, heartbeat, R1 usage and terminate on a running stack | `python -m conformance.rapp package my-rapp.csar`; `python -m conformance.rapp runtime --package-url URL --direct`; the report is `report.json` and `report.md` (`conformance/rapp/README.md`). The sample packages are signed with a demo key whose private half is public: never trust it |
| Mock O1 adaptor | `mock-o1-adaptor`: a stand-in network function with a trigger API, for the counterparty to compare their adaptor against and for a dry run | Compose or the chart (`mtls: off`) |
| Sample rApps | Energy saving, mobility optimization, coverage optimization, traffic steering: rApp packages (`samples/*.csar`) and the manifests that show the R1 services each needs | Onboard a CSAR; read `docs/RAPP_PACKAGING.md` |
| Images and chart | Signed images by tag (`ghcr.io/<owner>/<repo>/smo-<module>:<version>`), the Helm chart `deploy/helm/smo`, a compose stack, GitOps overlays | `docs/RELEASES.md`; a lab profile runs on one node |
| OpenAPI specs and call flows | `docs/openapi/` per module, `docs/call-flows/` per journey | Reference for the counterparty's integration work |
| Test evidence | `docs/VALIDATION.md` lists what is tested and what is not | Shared up front so that no one assumes more than is true |

### What we need from a counterparty

1. Which interface and role: a managed element or O1 adaptor (NETCONF over SSH or TLS, RESTCONF, which YANG modules), an rApp, or an O-Cloud IMS.
2. A reachable endpoint (VPN or a lab network), credentials by reference (`docs/SECRETS.md`: the credential is a name here, the value a mounted file), and for NETCONF over SSH the host key to pin.
3. The release of each specification they implement (YANG revisions, the O-RAN release), so a difference can be told from a defect.
4. A named contact for each side, a time window and a way to exchange logs and traces (`docs/OBSERVABILITY.md`).
5. For an rApp: the package, its manifest and the R1 services it calls.
6. A decision whether results may be recorded in this repository.

### Prerequisites

The stack runs from a tagged release (images signed, chart pinned) on a lab cluster or a single host; the counterparty's endpoint is reachable from RAN NF OAM (or FOCOM); a dry run of the conformance kit against the mock adaptor passes; the counterparty has read the compliance limits in `docs/STANDARDS.md` and the open items they touch; the secrets are supplied (`deploy/external-secrets`, or the chart's own).

### Entry criteria

* The release under test is tagged and the verification battery of `CLAUDE.md` passed for it.
* The kit runs clean against the mock adaptor with the same options that will be used against the counterparty.
* The test cases to run are listed beforehand and agreed in writing, with the specification and release each is taken from (the "to be confirmed" cells above are settled first).
* Both sides have the other's contact and a rollback: the counterparty's element can be returned to its previous configuration.

### Exit criteria

* Every agreed case has a recorded result (pass, fail with the evidence, not applicable with the reason); a failure is attributed to a side, or to an ambiguity in the specification, which is written down for the owning working group.
* The kit's report for the counterparty's adaptor is attached, with the options used.
* Defects found in this stack are in `OPEN_ITEMS.md` with an ID; known limits found in the counterparty are listed in the report and not argued.
* No credential, host name or personal data of either side is in the record (`docs/PRIVACY.md`).
* The result is summarised in one page for the owner: what was shown to interoperate, on which releases.

### Open questions for the owner

1. Which counterparty, and which interface first? O1 is where the repo has the most to offer (the kit); R1 and O2ims need the counterparty to be an rApp or an IMS.
2. Is a plugfest run as an O-RAN-organised event, or bilaterally? An organised event brings its own test specification and entry rules, which replace the criteria above.
3. Who holds the O-RAN specification access and decides which test cases apply to a reference SMO (the "to be confirmed" cells)?
4. Is flat REST addressing (no DN containment tree) acceptable to the counterparty, or is the tree a pre-condition?
5. May the images and chart be shared with a counterparty outside the organisation, and is the container registry reachable by them?
6. Is O2dms in scope at all, given that nothing claims NFO conforms to O2dms?
7. Where do results live: this repository, or a private record?
