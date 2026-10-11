/** English (the source language) — the messages every page shares: the sidebar's page names, which the Preferences page's start-page list also
 * shows, and the sentences of the shared helpers in `lib/` (`domain.`: lib/domain.ts, `mfa.`: lib/mfa.ts, GUI-6.4). A key is `<area>.<name>`; a
 * page's own messages are in `<page>.ts` beside this file (GUI-6.3, README.md). */

/** The shared messages, by key. */
export const common = {
  "nav./": "Dashboard",
  "nav./flows": "Lifecycle flows",
  "nav./rapps": "rApps",
  "nav./approvals": "Approvals",
  "nav./decisions": "Decisions",
  "nav./safeguards": "Safeguards",
  "nav./aiml": "AI/ML",
  "nav./policy": "Intents",
  "nav./alarms": "Alarms",
  "nav./kpis": "KPIs & Assurance",
  "nav./topology": "RAN topology",
  "nav./configuration": "Configuration",
  "nav./software": "Software",
  "nav./infrastructure": "Infrastructure",
  "nav./data": "Data & Exposure",
  "nav./preferences": "Preferences",
  "nav./security": "Account security",
  "nav./exports": "Exports",
  "nav./admin": "Admin",

  "domain.disposition.DIRECT": "Written at once: this rApp's changes are not held for approval",
  "domain.disposition.APPROVED": "Written after a person approved it",
  "domain.disposition.ROLLBACK": "An undo of an earlier job",
  "domain.disposition.REJECTED": "A person (or the timeout policy) rejected it: nothing was written",
  "domain.disposition.EXPIRED": "Nobody decided in time: nothing was written",
  "domain.disposition.REFUSED": "Approved, then refused by a safeguard or a check: nothing was written",
  "domain.integrity.VERIFIED": "The record still matches the hash written to the audit chain",
  "domain.integrity.UNCHAINED": "Not yet written to the audit chain (a moment after the job; the worker retries)",
  "domain.integrity.MISMATCH": "The record, or its audit row, was changed after it was written",

  "mfa.recoveryLeft_one": "{count} recovery code left.",
  "mfa.recoveryLeft_other": "{count} recovery codes left.",
  "mfa.recoveryLeft.none": "You have no recovery codes left: generate new ones on the Account security page.",
} as const;
