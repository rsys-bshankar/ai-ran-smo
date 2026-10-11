# rApp detail

Route: `/rapps/<instance>`    Design: handoff `RappDetail.dc.html`, SCALE.md "rApp detail"

Every word on the page comes from the catalog (`src/i18n/en/rapp-detail.ts`, `de/rapp-detail.ts`, GUI-6.4); its literal count is 0.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| rapp.header | sections/Header.tsx | name, version, owner, state, autonomy (read-only), Pin, Stop rApp (reason, confirm) / Resume | `/api/rapps/<id>`, `/api/me/pins`, `/rapp-mgmt/instances/<id>/safeguards`; `PUT\|DELETE …/kill`, `PUT\|DELETE /api/me/pins/<id>` | 15 s | 3 calls (shared) |
| rapp.kpis | sections/KpiTiles.tsx | headline KPI (first metric of the newest report, the next three in the foot), decisions 24 h, config jobs this hour vs. limit, performance reports, faults | `/rapp-mgmt/instances/<id>/performance/latest`; `/ran-nf-oam/decision-records?invoker_id=&since=&limit=1` (`total`); shared safeguards, performance, faults | 15 s | 2 calls |
| rapp.flows | sections/LifecycleFlows.tsx | flows 01, 06, 07 as one-line steppers (lib/flows.ts), each linking to `/flows/<id>?subject=`; flow 02 listed with a gap note | `/onboarding/packages/<pkg>/onboarding-status`, `/onboarding/packages/<pkg>/usage`, `/nfo/deployments/<workloadRef>`; shared instance, performance, faults | 15 s | 3 calls |
| rapp.history | sections/LifecycleHistory.tsx | committed upgrades and rollbacks, 10 at a time, Roll back | `/rapp-mgmt/instances/<id>/versions` | 15 s | 1 call |
| rapp.decisions | sections/RecentDecisions.tsx | latest 10 decisions, link to Decisions filtered (`/decisions?invoker=`) | `/ran-nf-oam/decision-records?invoker_id=&limit=10&total=false` | 15 s | 1 call |
| rapp.kpis-reported | sections/KpisReported.tsx | sparkline per reported metric | `/rapp-mgmt/instances/<id>/performance?limit=50` | 15 s | 1 call |
| rapp.instance | sections/InstanceState.tsx | the "Lifecycle" box: state, package, NFO deployment, pending upgrade, autonomy, region and access scope; lifecycle actions incl. Upgrade | `/rapp-mgmt/instances/<id>` | 15 s | 1 call |
| rapp.safeguards | sections/Safeguards.tsx | stopped or active, limits, config-jobs-per-hour meter | shared safeguards | 15 s | 0 |
| rapp.faults | sections/Faults.tsx | faults, server-paged | `/rapp-mgmt/instances/<id>/faults?limit=&offset=` | 15 s | 1 call/page |
| rapp.cells | sections/CellStateDistribution.tsx | gap note only | — | — | 0 |
| rapp.declared | sections/DeclaredPages.tsx | the operator page the package declares (`components/OperatorUi` DeclaredPage, unchanged) | the declared routes through `/api/rapps/<id>/operator/...` | per panel | per panel |

One rApp, so every read is bounded: about 13 calls on first load, most of them shared between boxes through one cache entry.

## Known limits

- The headline KPI is the first metric the rApp reported in its newest report (rApps declare no headline metric).
- Cell-state distribution / swimlane: the rApp's own data behind its declared operator API, not served by the platform (BRIEF §5); the cells box is a gap note.
- The autonomy segmented control is read-only: rApp Management has no route to change the mode of an instance (fixed at CreateInstance).
- Flow 02 for "the rApp's model" is not evaluated: the backend links no model to an rApp (packages declare no model id). The row links to the flow 02 board.
- The Decisions page must read `?invoker=` to arrive filtered; until it does, the link opens the unfiltered list.
- Refusals in the last 24 h per rApp: not shown here (see Safeguards).

## Troubleshooting

- A lifecycle row stays a skeleton: one of its reads failed (the box says so); the onboarding-status route answers 404 for a deleted package.
- "No credential": the instance is terminated; it has no invoker id, so no decisions, limits or stop.
- The declared page says the operator API is not registered: see `PUT /rapp-mgmt/instances/<id>/operator-api`.

## Upgrade notes

- Redesign: `pages/RappDetail.tsx` moved here. The declared operator page now renders below the platform overview (it was above).
