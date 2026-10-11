/** English (the source language) — the messages every page shares: the sidebar's page names, which the Preferences page's start-page list also
 * shows. A key is `<area>.<name>`; a page's own messages are in `<page>.ts` beside this file (GUI-6.3, README.md). */

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
} as const;
