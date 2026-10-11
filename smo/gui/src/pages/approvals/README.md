# Approvals

Route: `/approvals`    Design: `Approvals.dc.html` (BRIEF §4 "Approvals", PR-GUI-7)

Tabs (URL hash): `#waiting` (queue and detail side by side) · `#decided`. `ApprovalDrawer` (the detail in a drawer) is exported from
`index.tsx` for the Decisions page.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| approvals.queue | sections/Queue.tsx | waiting requests as cards with a lapse countdown bar and, for a request that needs two people, "1 of 2 approvals" ("one needed" otherwise); first one selected | `/ran-nf-oam/rapp-approvals?status=PENDING&total=false` | 5 s | 1 call per page |
| approvals.models | sections/ModelGates.tsx | GUI-7.3, tab Model gates: the models waiting for a governance decision (model, stage, the decision in words, `lib/domain.ts` GATE_DECISION), with the AI/ML page's decision buttons (`ModelActions`, role-gated: approving training or validation is operator, submit / approve / reject / certify admin); the model links to `/aiml?model=<id>#models`; tab count from the summary `modelGates.waiting` | `/aimgf/model-lifecycles?awaiting_decision=true` (server-paged), `/mlmr/models` (names, the AI/ML page's cache entry) | 5 s | 2 calls |
| approvals.windows | sections/ChangeWindows.tsx | GUI-7.1 (MGT-4), tab Change windows: CM jobs held for a change window or for approval (job, requester, window, asked), Approve / Reject (operator; the BFF names the decider, RAN NF OAM refuses the requester) | `/ran-nf-oam/config-jobs?status=PENDING_APPROVAL`, `POST …/{id}/approve\|reject` | 15 s | 1 call/page |
| approvals.scheduled | sections/ChangeWindows.tsx | approved jobs waiting for their window (window, approved by), Start (refused before the window opens) and Withdraw | `/ran-nf-oam/config-jobs?status=SCHEDULED`, `POST …/{id}/continue\|reject` | 15 s | 1 call/page |
| approvals.detail | sections/Detail.tsx | title, status, countdown clock, facts; hosts the boxes below | `/ran-nf-oam/rapp-approvals/{id}` (+ `/decision-records?approval_id=` once decided) | 5 s | 1–2 calls |
| approvals.impact | sections/ImpactTiles.tsx | changes, managed elements, access scope, what a lapse does | — (the request) | — | 0 |
| approvals.diff | sections/ChangeDiff.tsx | the config change as a diff (`kit/Diff`), 50 lines then "… N more" | — | — | 0 |
| approvals.votes | sections/Votes.tsx | two-person approval only: who has approved so far, when, with what reason ("Approvals so far (n of 2)"); nothing for a request that needs one | — (the request's `approvals`) | — | 0 |
| approvals.why | sections/Rationale.tsx | rationale, input chips, link to the decision record | — | — | 0 |
| approvals.decide | sections/DecisionBox.tsx | reason, Approve & write, Reject; then the result (status, config job). Two-person approval: says whether this is the first approval (nothing written yet) or the last one; Approve is disabled for someone who already approved (they can still reject) | `POST …/{id}/approve`, `POST …/{id}/reject` | — | 0 |
| approvals.decided | sections/Decided.tsx | decided and lapsed requests ("By" lists both approvers of a two-person request, `decidedByText`), outcome filter, drawer | `/ran-nf-oam/rapp-approvals?status&total=false` | 15 s | 1 call per page |

The tab badge is `approvals.PENDING` from `/api/summary/approvals` (never the length of a page).

## Known limits

- **Expected KPI impact** (handover success, ping-pong, model confidence) is not shown: the rApp sends no prediction with the request.
- **Safeguard check badges** are not shown: the request carries no check results (the checks run again on approve; a refusal shows as `REFUSED` with its code).
- **The diff has only "+" lines**: the request carries the new values, not the current ones. No server-side diff summary or "download full diff".
- "Approve similar for 1 hour", grouped queue, bulk approve of a group and Undo are not built (no backend support; BRIEF §5).
- Decided with "Every outcome": the route has no "not pending" filter, so pending rows of the page are left out in the browser.
- Change windows (GUI-7.1) list the held CM jobs and the approved ones waiting for their window; a scheduled job is started with Start in its window (starting by itself at the window, and expiring after it, are MGT-4.4 and 4.5).
- **Scope** (GUI-9.3): a request matches a region or site cluster when its change touches an element there (`managedElements`, recorded when it was parked); a request with no element is absent under any scope.
- **Two-person approval** (opt-in per rApp, Safeguards → Approval…): "you already approved" is decided in the browser by comparing the BFF's `smo-gui:<username>` voter name with the signed-in user; RAN NF OAM refuses a second approval by the same person anyway.

## Troubleshooting

- Approve is greyed out on a two-person request: you gave its first approval; a different person must give the second.
- No Approve / Reject: the role lacks `POST /ran-nf-oam/rapp-approvals/{id}/approve` (operator and up), or the request is no longer pending.
- Approve answers `REFUSED`: a safeguard (stopped rApp, a limit) refused it when it ran; see Safeguards → Refusals.
- Queue empty but the badge counts: the summary is cached 5 s by the BFF; it catches up on the next refresh.
