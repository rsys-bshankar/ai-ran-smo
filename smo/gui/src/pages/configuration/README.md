# Configuration

Route: `/configuration` (tabs in the hash: `#jobs`, `#new`, `#vendors`, `#trust`, `#onboarding`; `?job=<id>`, `?status=<state|all>`)    Design: handoff `Configuration.dc.html` (BRIEF §4e features 3 and 11)

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| configuration.tiles | sections/ConfigTiles.tsx | halted, running, completed, partial/failed, all (tiles filter the list) | `/api/summary/configuration` (`configJobs.*`) | 15 s | 1 call (shared with the tab count and the list default) |
| configuration.jobs | sections/JobList.tsx | config jobs, server-paged; halted first (default filter while the summary counts any) | `/ran-nf-oam/config-jobs?status=&limit=&offset=` | 15 s | 1 call/page |
| configuration.job | sections/StagedJob.tsx (+ KpiGuard.tsx, JobRollback.tsx) | waves (currentWave of waveCount, each wave's outcome), pause countdown, haltedReason, Continue now / Halt / Abort, Roll back (preview first), KPI guard and result, Run KPI check (operator), sub-change counts, first 50 sub-changes; "Open job" opens `components/ConfigJobDrawer` | `/config-jobs/{id}`; POST `/{id}/continue|halt|abort|rollback|kpi-check` | 5 s | 1 call when a job is open |
| configuration.new | sections/NewJobForm.tsx | elements, function ref, access scope, operation, attribute JSON, waves, gate, on gate failure halt / revert, KPI guard, hold for approval with an optional change window (MGT-4: the job then waits in Approvals → Change windows), dry run with verdicts | POST `/config-jobs` (`dryRun` or not); `/kpi-definitions?limit=100` | — | 1 call |
| configuration.vendors | sections/Vendors.tsx | vendor capabilities: services, conformance SPEC / OWN / COMBINED, modes, schema refs | `/vendor-capabilities` | 15 s | 1 call/page |
| configuration.schemas | sections/CmSchemas.tsx | CM schemas: YANG / OPENAPI_NRM / DESCRIPTOR, revision, classes, location | `/cm-schemas` | 15 s | 1 call/page |
| configuration.trust | sections/HostKeys.tsx | O1 endpoints (health filter); the selected ssh endpoint's pinned host keys, alert when none is pinned or it is unreachable; admin: pin a key (paste a blob, `.pub` or known_hosts line), Remove | `/o1-adaptor-endpoints?health_status=`, `/o1-adaptor-endpoints/{id}/host-keys`; PUT `…/host-keys`, DELETE `…/host-keys/{keyType}` | 15 s / 60 s | 1 call/page + 1 |
| configuration.templates | sections/OnboardingTemplates.tsx | onboarding templates (MGT-14.6): applies to (type · vendor), changes, software baseline (required or flagged), applied on first heartbeat or by an operator, enabled; admin: New template… / Edit / Delete (asks first); others: a read-only view. The form is checked by `templatePayload` (lib/lifecycle.ts) before the call | `/onboarding-templates?limit=200`; admin PUT / DELETE `/onboarding-templates/{name}` | 15 s | 1 call |
| configuration.onboarding | sections/ElementOnboarding.tsx (+ OnboardingDialogs.tsx) | discovered → selected → applying → onboarded, software check (version / baseline, "not reported"), template, why it failed, config job; **Apply** / **Apply again** and **Select…** only where `onboardingActions` allows (operator), "Select a template for an element…" for an element with no row yet; a row opens the detail drawer (meaning, full detail, the config job in a second drawer) | `/element-onboarding?status=&software_check=`, `/element-onboarding/{me}` (drawer, 5 s); POST `/{me}/select` (`template`, `softwareVersion` optional), `/{me}/apply` (`softwareVersion` optional); `/o1-adaptor-endpoints?limit=200` (element picker) | 15 s | 1 call/page |
| configuration.watchers | ../software/sections/LifecycleWatchers.tsx | who is told when an onboarding fails, a campaign halts or a rollback fails (PR-MGT-14.7); admin: Add watcher (callback URL, events) / Remove | `/lifecycle-subscriptions`; admin POST, DELETE `/{id}` | 15 s | 1 call |

Only the visible tab's boxes load. Every action is a role-gated `ActionButton` / `Can` against `src/auth/permissions.fixture.json`
(the mirror of `gui-bff/app/rbac.py`); the BFF sets `requestedBy` (and an admin's MSAC tier).

## Known limits

- **Run KPI check** repeats the job's own declared guard; checking a job without a guard, or with other settings, is API-only.
- **Host-key mismatch**: the backend refuses a changed key but records no mismatch, so the page cannot say "MISMATCH". It alerts when an ssh endpoint
  has no pinned key, and says a changed key is one possible cause when the endpoint is UNREACHABLE.
- Declaring a vendor capability or loading a CM schema (admin, `PUT /vendor-capabilities/{v}`, `POST /cm-schemas`) are API-only here: the
  boxes read. "Used by" per schema (mockup) is not served.
- The job list rows carry no wave progress or requester label beyond `requestedBy` (the list route does not return waves); the detail does.
- Sub-changes of a job come in one answer (the job detail is not paged); the box counts them and lists the first 50, the drawer lists all.
- **Scope** (GUI-9.3): config jobs (any target element in the scope), O1 endpoints and element onboarding follow the top bar's scope; vendor capabilities and CM schemas are not tied to an element and are network-wide.

## Troubleshooting

- The list is empty though jobs exist: the default filter is HALTED while the summary counts halted jobs; pick "All".
- Continue answers 409 `WAVE_PAUSE_NOT_ELAPSED`: the page sends `force` only when it saw the pause running; reload and retry.
- A new job answers 403 `MSAC_ACCESS_DENIED` with scope entire-RAN: only an admin holds the MSAC tier.
- Dry run says `WOULD_REJECT_SOME` with `ENDPOINT_UNREACHABLE`: the element's O1 endpoint is down or not registered (Endpoint trust tab).

## Upgrade notes

- v1 (GUI redesign): new page. Its New job tab is a fuller form for the same `POST /config-jobs` as the CM write form on Infrastructure → O1.
- Merge of main's MGT-14.6/14.7 (#402): the pre-redesign Infrastructure → Onboarding tab became this page's Element onboarding tab (templates, the element box's Apply / Select dialogs and drawer, the watchers); Apply now opens a dialog for the software version instead of asking a yes/no question.
