# rApps

Route: `/rapps` (tabs in the hash: `#directory` (default), `#instances`, `#packages`, `#rollouts`)    Design: handoff `Rapps.dc.html`, SCALE.md "rApps · at scale"

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| rapps.tiles | sections/SummaryTiles.tsx | running / upgrading / faulted / deploying instances, packages | `/api/summary/rapps` | 15 s | 1 call (shared by the tab pills, pipeline chips and strip) |
| rapps.pinned | sections/PinnedAttention.tsx | pinned, then FAULTED and UPGRADING rApps, max 6 cards, each with its headline KPI | `/api/me/pins` (shared with the sidebar); `/api/rapps?state=FAULTED\|UPGRADING&limit=6` only when the summary counts any; `/rapp-mgmt/instances/performance/latest?ids=` for the cards | 15 s | 0–3 calls |
| rapps.directory | sections/Directory.tsx | every rApp, search, state / owner / page / pinned filters, star to pin | `/api/rapps` (BFF, server paging) | 15 s | 1 call/page |
| rapps.instances | sections/InstanceTable.tsx (+ InstanceDrawer, InstanceActions, UpgradeModal, VersionHistory, LifecycleCell, HeadlineKpi) | instance server table, state filter, flow 07 lifecycle column, headline KPI column | `/rapp-mgmt/instances?state=&limit=&offset=`; package names `/onboarding/packages?limit=500`; `/rapp-mgmt/instances/performance/latest?ids=<the page's ids>` | 15 s / 60 s | 3 calls |
| rapps.onboard | sections/OnboardForm.tsx | onboard a CSAR: upload the file, or give a location | `POST /onboarding/packages/upload` (multipart), `POST /onboarding/packages` | — | 0 |
| rapps.packages | sections/PackagesTable.tsx (+ PackageDrawer, CreateInstance) | package pipeline counts (filter chips), package server table, Deploy, lifecycle calls | summary; `/onboarding/packages?state=` (`include_deleted=false` for All states: a deleted package is shown only under DELETING) | 15 s | 1 call/page |
| rapps.rollouts | sections/Rollouts.tsx | instances UPGRADING, with resolve actions | `/rapp-mgmt/instances?state=UPGRADING` | 15 s | 2 calls |

First load on `/rapps`: summary, pins, directory (3; the session and permission reads are the shell's). `InstanceActions` and `VersionHistory`
are also used by `pages/rapp-detail`.

The lifecycle column (`data/lifecycle.ts`) maps the row's own state onto five segments of flow 07 (deploy, run, fault/recover, upgrade,
terminate) and links to `/flows/07?subject=<instance>`; it costs no call.

Every word on the page comes from the catalog (`src/i18n/en/rapps.ts`, `de/rapps.ts`; GUI-6.4); its literal count is 0.

## Known limits

- Autonomy mix, actions in 24 h and refusals per rApp: not served by the backend (no per-mode count, no per-rApp decision/refusal count in the summary). Tiles say so.
- The headline KPI is the first metric of an rApp's newest performance report (rApps declare no headline metric); the batched read covers at most
  50 instances, so a 100-row page shows it for the first 50. A per-rApp health meter is not served; not shown.
- Directory grouping by category: packages declare no category the backend serves.
- Facets (autonomy mode, owner, vendor) and bulk actions on the instance table: rApp Management's list filters by `state` only. The Directory tab has owner and search through the BFF.
- Rollouts: no canary or wave progress; an upgrade is one replacement instance, resolved as a whole. The replacement id is in the instance drawer (the list row does not carry it).
- Package names in the instance table come from one read of the newest 500 packages; past that a row shows the package id.
- The autonomy mode cannot be changed: rApp Management has no route for it (it is fixed at CreateInstance).
- **Scope** (GUI-9.3): rApp instances follow the top bar's region (the instances authorised for it, plus those with no region scope: `include_unscoped`, rApp Management's default); packages know no region and their tile says "network-wide" under a scope.
- The package drawer's signature says what Onboarding reports (`signatureVerified`: verified against its trust store, or accepted unsigned);
  Onboarding serves no trust-store mode and no signer name, so neither is shown (GUI-10.2).

## Troubleshooting

- Tiles show "—": the summary's module did not answer; the tile row says "Partial: …". Check `/modules/status`, then rapp-mgmt / onboarding logs.
- The strip is empty while instances are FAULTED: the strip reads `/api/rapps?state=FAULTED`; check the GUI BFF (gui-bff/app/rapps.py) can reach R1 / rApp Management.
- A table shows "Refresh failed … updated N s ago": the page kept the last good page; the module is down.

## Upgrade notes

- Redesign: `pages/Rapps.tsx` and `pages/RappDirectory.tsx` moved here; the default tab is still Directory (scripts/gui_rapp_pages_e2e.py relies on it), old `#directory|#packages|#instances` links still work, `#rollouts` is new.
