/** English — the redesign's shared building blocks: diff, pager, meters, table and box states (`kit`), GUI-6.4. */

/** The messages, by key. */
export const kit = {
  "kit.diff.label": "Change",
  "kit.diff.none": "no change",
  "kit.diff.more": "… {n} more lines",
  "kit.meter.usedOf": "{used} of {limit}",
  "kit.pager.aria": "Pages",
  "kit.pager.showing": "Showing {from}–{to}",
  "kit.pager.of": "of",
  "kit.pager.more": "more",
  "kit.pager.rows": "Rows",
  "kit.pager.rowsAria": "Rows per page",
  "kit.pager.previous": "← Previous",
  "kit.pager.next": "Next →",
  "kit.scopeNote": "network-wide",
  "kit.scopeNote.title": "The scope picked in the top bar does not narrow this: it counts the whole network",
  "kit.table.refreshFailed": "Refresh failed: {error}",
  "kit.boundary.failed": "This box failed to render: {error}",
  "kit.retry": "Retry",
  "kit.loading": "Loading",
  "kit.empty": "Nothing here yet.",
  "kit.stale.updated": "updated {age} ago",
  "kit.stale.minutes": "{n} min",
  "kit.stale.seconds": "{n} s",
} as const;
