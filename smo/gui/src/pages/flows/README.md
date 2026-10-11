# Lifecycle flows

Routes: `/flows` (first flow), `/flows/:flowId`, subject in `?subject=<id>`; an old `/flows#07` link opens flow 07.    Design: handoff `Flows.dc.html`, `Flow02…Flow19.dc.html`

Every word on the page comes from the catalog (`src/i18n/en/flows.ts`, `de/flows.ts`; GUI-6.4), except what `lib/flows.ts` supplies (flow titles, subject nouns, step texts), which stays English; its literal count is 0.

## Sections

| id | file | what it shows | API | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| flows.list | sections/FlowList.tsx | the catalogue (`lib/flows.ts` FLOWS), links to `/flows/<id>`; progress bar for the selected flow and subject only | — | — | 0 |
| flows.board | sections/FlowBoard.tsx (+ ModuleChips, SubjectPicker) | title, doc, modules, the subject combobox | the flow's subject list (`data/flowNN.tsx`) | 15 s | 1 call |
| flows.subject | sections/SubjectPicker.tsx | typeahead over the loaded subjects, recent 5 first (localStorage `smo.flows.recent`) | — | — | 0 |
| flows.lanes | sections/SequenceLanes.tsx | SVG: actors from `FlowStep.actor` as columns, each step an arrow, current dashed, failed red | — | — | 0 |
| flows.steps | sections/StepTimeline.tsx | `kit/Timeline` of the steps, phases for flow 02, the flow's actions | the flow's sources for the subject | 15 s | 1–5 calls |
| flows.funnel | sections/FleetFunnel.tsx | state counts that match steps (`data/funnel.ts`) | `/api/summary/<page>` | 15 s | 0–1 call |

Per-flow data (`data/flowNN.tsx`, registry `data/boards.ts`), first-load calls including the subject list:

| flow | subject | sources | calls |
| --- | --- | --- | --- |
| 01 | package | packages, instances (to find the package's), instance, NFO deployment | 4 + funnel |
| 02 | model | models, lifecycle, training jobs, inference jobs, MLMF subscriptions, reports per subscription | 5 + subs |
| 03 | config job | O1 endpoints, config jobs, the job | 3 + funnel |
| 04 | monitor | monitors, order, remedial actions, MDAF / MLMF report totals (one-row pages) | 5 |
| 06 | package | packages, usage | 2 + funnel |
| 07 | instance | instances, instance, performance (50), faults (50) | 4 + funnel |
| 08 | analytics type | producers, subscriptions, reports, SME APIs per producer | 3 + producers |
| 09 | intent | intents, handlers, reports | 3 + funnel |
| 10 | service order | orders | 1 |
| 15 | NF deployment | deployments, LCM operations | 2 + funnel |
| 16 | O-Cloud resource | pools, resources per pool, inventory subscriptions | 2 + pools |
| 19 | software job | software management jobs | 1 |

Evaluators stay pure in `lib/flows.ts` (tested in `lib/flows.test.ts`): flows 15, 16, 19 and `flow02Phases` (02 + 17 + 26) were added with
the redesign; `toStepState` maps a step status onto the kit's states.

## Known limits

- Fleet funnel: only flows 01, 03, 06, 07, 09 and 15 have one, built from summary state counts. Flows 02, 04, 08, 10, 16, 19 need a per-step server aggregate (SCALE.md §5); the box shows a gap note. "Click a step to page the subjects stuck there" is not built.
- Subject lists read the newest 500 (the backend's page maximum); typeahead is over those, not a server search.
- Flow 01 finds a package's instance among the newest 500 instances (rApp Management has no package filter).
- Flow 16: "resource in use by deployments" is not a step (FOCOM does not record it); "notified" means a matching subscription existed (delivery is best effort and not recorded). A deprovisioned resource disappears, so it cannot be followed.
- Flow 15: a synchronous terminate deletes the deployment row, so DELETED is never observable here.
- Flow 02: rollback is optional and cannot be proved without the governance history; it stays "todo" with an action when the model is PROMOTED.

## Troubleshooting

- "No <subject> to follow yet": the subject list is empty; the empty state links to where one is made.
- A step stays current although it happened: the evaluator in `lib/flows.ts` reads a field the backend changed; see its test.
- The progress bar in the list shows only for the open flow: by design (other flows' subjects are not loaded).

## Upgrade notes

- Redesign: `pages/Flows.tsx` moved here; flows are routes (`/flows/<id>`) instead of hash tabs; flows 15, 16, 19 and flow 02's phases are new.
