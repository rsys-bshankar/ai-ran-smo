/** English — the Exports page (`pages/exports`), GUI-6.4. */

/** The page's messages, by key. */
export const exports = {
  "exports.eyebrow": "Account · files",
  "exports.title": "Exports",
  "exports.subtitle.before": "CSV files written in the background from ",
  "exports.subtitle.decisions": "Decisions",
  "exports.subtitle.after": " and the Admin audit log; each is kept 24 hours after it finishes",

  "exports.list.title": "Your exports",
  "exports.list.running_one": "{count} running · refreshing every 2 s",
  "exports.list.running_other": "{count} running · refreshing every 2 s",
  "exports.list.newestFirst": "newest first",
  "exports.list.allUsers": "All users",
  "exports.list.filterByUser": "Filter by user",
  "exports.list.empty": "No export yet. Use “Export…” on Decisions, Alarms or the Admin audit log.",
  "exports.list.col.what": "What",
  "exports.list.col.filters": "Filters",
  "exports.list.col.requested": "Requested",
  "exports.list.col.state": "State",
  "exports.list.col.rows": "Rows",
  "exports.list.col.size": "Size",
  "exports.list.col.finished": "Finished",
  "exports.list.col.keptUntil": "Kept until",
  "exports.list.noFilters": "none",
  "exports.list.download": "Download",
  "exports.list.delete": "Delete",
  "exports.list.confirmStop": "Stop and delete this export?",
  "exports.list.confirmDelete": "Delete this export and its file?",
  "exports.list.span": "{from} → {until}",
  "exports.list.firstRecord": "first record",
  "exports.list.requestTime": "request time",

  "exports.kind.decisions": "Decision records",
  "exports.kind.alarms": "Alarms",
  "exports.kind.audit": "Audit log",
} as const;
