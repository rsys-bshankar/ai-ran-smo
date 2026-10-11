# Safeguards

Route: `/safeguards`    Design: `Safeguards.dc.html`, SCALE.md "Safeguards"

Tabs (URL hash): `#limits` · `#refusals` · `#watchers`. "Stop all rApp writes" sits in the page header.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| safeguards.stopall | sections/GlobalStop.tsx | Stop all rApp writes (operator): confirm with live / already-stopped counts and a reason, one call, result (stopped, already stopped, failed list); Resume all (admin) | `GET /rapp-mgmt/kill-all` (on click), `PUT /rapp-mgmt/kill-all`, `DELETE /rapp-mgmt/kill-all` | — | 0 on load |
| safeguards.tiles | sections/SummaryTiles.tsx | instances, stopped, refusals 24 h | `/api/summary/rapps`, `/ran-nf-oam/rapp-kill?limit=1`, `/ran-nf-oam/safeguard-refusals?since&limit=1` | 15 s | 3 calls |
| safeguards.limits | sections/LimitsTable.tsx (+ Dialogs.tsx) | per-rApp table: mode, writes, change/write, jobs/h used vs max, elements/job, hold ("two different people must approve" for a two-person policy), actions; the Approval… dialog's *Approvals needed* (one by default, or two different people: sends `requiredApprovals: 2` only then) | `/rapp-mgmt/instances`, `/rapp-mgmt/instances/{id}/safeguards` per row | 15 s | 1 + 1 per row |
| safeguards.detail | sections/RappDetail.tsx | selected rApp: stop details, meters, limits, approval, refusals 24 h | same safeguards call, `/ran-nf-oam/safeguard-refusals?invoker_id&since&limit=1` | 15 s | 1 call |
| safeguards.stopped | sections/Stopped.tsx | everything stopped at RAN NF OAM | `/ran-nf-oam/rapp-kill` | 15 s | 1 call |
| safeguards.refusals | sections/Refusals.tsx | refusals, range 24 h / 7 d (default) / 30 d / all, reason, invoker | `/ran-nf-oam/safeguard-refusals?code&invoker_id&since` | 15 s | 1 call |
| safeguards.watchers | sections/Watchers.tsx | webhooks told about refusals, Add / Remove | `/ran-nf-oam/safeguard-subscriptions` | 15 s | 1 call |

Every word on the page comes from the catalog (`src/i18n/en/safeguards.ts`, `de/safeguards.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **Stop all** keeps the first stop of an rApp already stopped (its reason and author are not replaced). **Resume all** resumes every stopped
  rApp, whoever stopped it; there is no "resume only what stop-all stopped".
- Each table row reads its instance's safeguards (one call per row of the page): there is no list route of safeguards.
- The refusal count per rApp is in the detail card only (one more call per row would double the page's calls).
- **Scope** (GUI-9.3): the rApp instances follow the top bar's region (instances authorised for it, plus the unscoped ones); the refusals
  record no managed element, so `/safeguard-refusals` cannot be scoped and the refusal card says "network-wide" under a scope.
- "Near their rate limit" and "holding for approval" tiles, table filters on them, and bulk "hold selected" are not built: no server count or filter.

## Troubleshooting

- A row says "no credential": the instance is terminated, nothing to stop or limit.
- Stop all reports failures: each failed instance is listed with the error its stop call answered.
- No Approval… / Stop holding: setting an approval policy is an admin's decision.
