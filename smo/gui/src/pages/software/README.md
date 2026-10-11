# Software

Route: `/software` (tabs in the hash: `#campaigns`, `#jobs`, `#new`; `?campaign=<id>`, `?wave=<n>`, `?status=<state>`)    Design: handoff `Software.dc.html` (BRIEF §4e feature 2)

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| software.tiles | sections/CampaignTiles.tsx | running · halted (filters the list), completed, rolled back, all with a state meter | `/api/summary/software` (`campaigns.*`) | 15 s | 1 call (shared with the tab count) |
| software.list | sections/CampaignList.tsx | campaigns, server-paged, state filter | `/ran-nf-oam/software-campaigns?status=&limit=&offset=` | 15 s | 1 call/page |
| software.detail | sections/CampaignDetail.tsx (+ CampaignActions.tsx, Countdown.tsx) | selector and settings, halted reason (GATE_FAILED / WAVE_PAUSE with countdown / OPERATOR_HALT), Continue / Halt / Roll back / Abort, totals, wave strip, event log | `/software-campaigns/{id}`, `/software-campaigns/{id}/report`; POST `/{id}/continue|halt|abort|rollback` | 5 s / 10 s | 2 calls when a campaign is open |
| software.elements | sections/CampaignElements.tsx | the elements of one wave, each job linking to `/flows/19?subject=<job>` ("timed out" when the job timeout failed it), each element to `/elements/<me>` | the report (shared) | 10 s | 0 extra |
| software.watchers | sections/LifecycleWatchers.tsx | who is told when a campaign halts, a rollback fails or an onboarding fails (PR-MGT-14.7, MGT-15.6); admin: Add watcher (callback URL, events) / Remove. Also shown on Configuration → Element onboarding | `/ran-nf-oam/lifecycle-subscriptions`; admin POST, DELETE `/{id}` | 15 s | 1 call |
| software.jobs | sections/ElementJobs.tsx | every element software job (flow 19), server-paged, element filter | `/software-management-jobs?managed_element_ref=` | 15 s | 1 call/page |
| software.new | sections/NewCampaignForm.tsx (+ data/form.ts, the GUI's one statement of RAN NF OAM's campaign rules: name ≤ 200, version ≤ 100, at most 5000 named elements) | selector or element list, wave size, pause, gate, on gate failure, job timeout (MGT-15.6: blank waits, 1 s to 7 days) and rollback order (MGT-15.7: last wave first by default, or all at once); dry run first, then Start | POST `/software-campaigns` (`dryRun: true`, then without); `/vendor-capabilities?limit=100` for vendor suggestions | — | 1 call |

Actions are role-gated (`ActionButton`, `Can`; operator in `gui-bff/app/rbac.py`, which also sets `requestedBy`). Roll back (naming the campaign's rollback order) and Abort ask
first; Continue during an unexpired pause asks and sends `force: true`. `campaignActions` (data/types.ts) mirrors the states the backend accepts.

Every word on the page comes from the catalog (`src/i18n/en/software.ts`, `de/software.ts`; GUI-6.4); its literal count is 0. The lifecycle watcher card
(`sections/LifecycleWatchers.tsx`) takes its words from there on the Configuration page too.

## Known limits

- The mockup's tiles "Elements on 24.3.x %", "Gate failures · 7 d" and "Element jobs failed · 24 h" are not shown: no route serves a
  software-version share, gate failures over time or failed jobs by time (the jobs list has no time or status filter).
- Elements per wave come from the campaign report, which holds every wave of the campaign in one answer; the box pages them 50 at a time in the
  browser. A paged per-wave route would be needed for campaigns of many thousands of elements.
- The element job list has no status or time filter (the route takes only `managed_element_ref`), so "failed first" is not offered.
- "New alarms" per element (mockup column) is not served; the gate's own detail says how many alarms failed it.
- Region, tenant and entity type in the form are free text: no route lists the values in use.
- **Scope** (GUI-9.3): a campaign matches when its selector names the region or any of its element jobs is in the scope; software management jobs are network-wide.

## Troubleshooting

- Continue answers 409 `WAVE_PAUSE_NOT_ELAPSED`: the pause has not ended; the button sends `force` only when the page saw the pause running.
- Roll back answers 422 `ROLLBACK_NOT_POSSIBLE`: a job of the campaign is still running; wait for it (the detail refreshes every 5 s).
- Start stays disabled: run "Dry run" with the current values first (any change to the form needs a new dry run).

## Upgrade notes

- v1 (GUI redesign): new page.
- Merge of main's MGT-15.5 to 15.7 (#402): the pre-redesign Infrastructure → Software campaigns tab is this page; its job timeout, rollback order, "timed out" marking and the failure-notice watchers were added here.
