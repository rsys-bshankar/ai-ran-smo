# Decisions

Routes: `/decisions` (`?job=<id>` / `?approval=<id>` narrow it) and `/decisions/:decisionId`    Design: `Decisions.dc.html` (PR-AI-13.4)

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| decisions.filters | sections/FilterBar.tsx | rApp, outcome, model version, range 1 h / 24 h (default) / 7 d / all, until; "Export…" (operator) | — (builds the query); "Export…" creates a job `POST /api/exports {kind: decisions, since, until, invokerId, disposition, region, siteCluster}` (GUI-9.5b, `components/ExportJobButton.tsx`), followed on `/exports` | — | 0 (1 on click) |
| decisions.tiles | sections/SummaryTiles.tsx | decisions in 24 h, autonomous, approved by a person, not written | `/api/summary/decisions` | 15 s | 1 call |
| decisions.table | sections/DecisionTable.tsx | records ("Approved by" lists both `approvers` of a request that needed two), keyset-paged (`kit/KeysetTable`, Next / Previous, no count); row click selects | `/ran-nf-oam/decision-records?invoker_id&disposition&model_version&job_id&approval_id&since&until&region&site_cluster&after&limit` (answer `{items, nextCursor, hasMore}`) | 15 s | 1 call per page |
| decisions.chain | sections/DecisionChain.tsx | inputs → model → rationale → config job → approval → verify | — (the selected row) | — | 0 |
| decisions.integrity | sections/Integrity.tsx | record hash, audit row, VERIFIED / UNCHAINED / MISMATCH | `/ran-nf-oam/decision-records/{id}` | 15 s | 1 call per selected row |
| decisions.outcome, decisions.why | sections/RecordDetail.tsx | the one-record route: outcome (job and approval drawers; "Approvers (two were needed)" when the record carries `approvers`), why | same record | — | 0 |

Every word on the page comes from the catalog (`src/i18n/en/decisions.ts`, `de/decisions.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **Verify** (the sixth step) shows a gap note: the record carries no verification result.
- **Integrity** is per record (the record against its own audit row). The API serves no "previous hash" and no whole-chain status, so there is
  no "chain intact" claim; the box names `python -m smo_shared.audit verify`.
- **Export** is an asynchronous job (GUI-9.5b): no span limit ("All" exports from the first record), at most 10,000,000 rows, the file kept
  24 h on the Exports page. Model version, job and approval narrow the table only, not the file (the export source takes no such filter).
- **Scope** (GUI-9.3): a record matches a region or site cluster when any of its `managedElements` is there; a record with no element never
  matches, so it is absent under any scope. The 24 h tiles are narrowed the same way.
- Keyset paging goes forward by cursor and back through the cursors already seen; there is no jump to a page number and no total.
- "Rolled back" has no 24 h summary count, so the fourth tile is "not written" (rejected + lapsed + refused) instead.

## Troubleshooting

- Nothing in the list: the default range is the last 24 h; pick "All".
- A `?job=` link shows nothing: the job has no record (a job a person made), or the record is older and still being chained.
- Integrity `UNCHAINED`: the audit worker has not written the row yet; it retries.
