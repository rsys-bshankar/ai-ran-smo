# Intents

Route: `/policy` (component `Policy`)    Design: handoff `Intents.dc.html` (BRIEF §4 Intents, §4e feature 10; SCALE.md "Intents")

TS 28.312 intents dispatched to intent handlers. Tabs (URL hash): `intents` (default), `handlers`, `autonomy` (dispatches), `formulas`.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| intents.tiles | sections/SummaryTiles.tsx | activated / total, deactivated (summary); not fulfilled, in conflict (server totals; click to filter the table) | `/api/summary/intents`; `/intent-service/intents?fulfilled=false&limit=1`, `?in_conflict=true&limit=1` | 15 s | 3 calls |
| intents.table | sections/IntentTable.tsx | intents, server-paged, `admin_state` and fulfilment (`fulfilled`, `in_conflict`) filters, fulfilment % and conflict badge; table or cards | `/intent-service/intents` | 15 s | 1 call/page |
| intents.card | sections/IntentCard.tsx | one intent: fulfilment ring (`fulfilmentPercent`), expectation, handler, actions, conflicts, negotiation feedback | `/intent-service/intent-reports?intent_id=&limit=20` | 15 s | 1 call per card |
| intents.reports | sections/IntentReports.tsx | drawer: facts, expectations, every report, admin "publish a report" | `/intent-service/intent-reports?intent_id=&limit=100` | 15 s | 1 call |
| intents.new | sections/NewIntentForm.tsx | new intent with a live handler-support check | `/intent-service/intent-handling-functions?limit=200` | 60 s | 1 call |
| intents.handlers | sections/Handlers.tsx | handlers, server-paged, deregister, admin register | `/intent-service/intent-handling-functions` | 15 s | 1 call/page |
| intents.dispatches | sections/Dispatches.tsx | autonomy dispatches, `status` filter, resolve / reject, request form | `/intent-service/autonomy-dispatches`, `/rapp-mgmt/instances` | 15 s | 1–3 calls |
| intents.formulas | sections/UtilityFormulas.tsx | TS 28.312 utility formulas (read-only) | `/intent-service/intent-utility-formulas` | 15 s | 1 call/page |

First load of the Intents tab: summary, the two filtered totals, one intents page, the handlers (new-intent form, operators) = 5 calls; selecting
an intent adds 1.
The cards view reads one page of 6 intents and each card's reports (7 calls).

Every word on the page comes from the catalog (`src/i18n/en/intents.ts`, `de/intents.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **Fulfilment percent** is Intent Service's `fulfilmentPercent` (share of targets/expectations its newest fulfilment report marks fulfilled);
  against an older Intent Service the ring falls back to counting the targets of the newest report in the browser.
- **Conflicts** are the ones a handler reported; there is no server-side conflict detection across intents. The new-intent form's overlap check
  is not done for the same reason. The callout lists the conflicts of the newest conflict report among the card's 20 newest reports.
- **Negotiation feedback**: the satisfaction index the buttons send is 30 / 70 / 100 (TS 28.312 leaves the scale to the consumer).
- **Utility formulas** are read-only: the BFF exposes no write on `/intent-utility-formulas`. "Used by N intents" is not served.
- **Scope** (GUI-9.3): intents know no region; under a scope the tiles say "network-wide".

## Troubleshooting

- A card's ring shows "—": the handler has not published a fulfilment report yet (Reports shows what it did publish).
- Activate / Deactivate is missing: only the intent's creator may change its admin state; intents created here carry RMIO `smo-gui`.
- "Create intent" answers 422: the handler's capabilities do not cover the object type, a target or the scope; the support badge says which.
