# Exports

Route: `/exports` (operator and admin; sidebar group Account)    Design: none in the hand-off (GUI-9.5b, `OPEN_ITEMS.md` PR-GUI-9)

The asynchronous CSV exports of the console. "Export…" on **Decisions** and on the **Alarms** table (operator, GUI-2.5) and on **Admin → Audit log** (admin) opens a dialog that
lists what the file will hold, then creates a job with the page's filters and the global scope (`POST /api/exports`, 202 at once); the BFF writes
the file in the background, page by page by keyset, and keeps it 24 hours after it finishes. This page follows the jobs and downloads the files.
Rules, types and the request builders: `src/data/exports.ts`; the button and dialog: `src/components/ExportJobButton.tsx`.
Every word on the page comes from the catalog, `src/i18n/en/exports.ts` and `de/exports.ts` (GUI-6.4).

## Sections

| id | file | what it shows | API | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| exports.list | sections/ExportList.tsx | the jobs, newest first: what (decision records / alarms / audit log) and the span, filters, requested (and, for an admin, by whom), state (and the error of a failed one), rows, size, finished, kept until; Download (DONE), Delete (asks first; stops a running one); an admin can narrow to one user | `GET /api/exports?limit=100[&username=]`, `GET /api/exports/{id}/file` (the download link), `DELETE /api/exports/{id}` | 2 s while a job is QUEUED or RUNNING, else 30 s | 1 call |

The dialog's request (`data/exports.ts`): decisions take the range (`since`, `until`), rApp (`invokerId`), outcome (`disposition`) and the scope
(`region`, `siteCluster`); alarms (`alarmsExport`) the alarm table's severity, managed element and function, ack state, probable cause and open
only, with the scope, from the first alarm (the table has no time filter); the audit log its user, event and range. "All" sends `since` = the Unix epoch: a job has no span limit (at most
10,000,000 rows). Errors are said in the dialog: 429 (three of the user's exports already running), 403 (role), 422 (refused filter).

## Known limits

- The model version, job and approval filters of the Decisions page narrow the table only; the decision export source takes no such filter (the
  dialog says so when one is set).
- The list shows the newest 100 jobs; older ones (each at most 24 h after it finished) are not paged.
- A job runs in the BFF instance that accepted it; if that instance stops, the job reads FAILED "interrupted" and must be started again.

## Troubleshooting

- A job stays QUEUED: the BFF instance that accepted it is busy or stopping; it reads FAILED "interrupted" once its heartbeat is stale.
- Download answers 410: the file expired (24 h after it finished) and was purged; export again.
