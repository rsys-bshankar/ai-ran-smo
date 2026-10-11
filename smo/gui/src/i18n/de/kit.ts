/** German — the shared building blocks (see `../en/kit.ts`). */
import type { MessageKey } from "../en";

export const kit: Partial<Record<MessageKey, string>> = {
  "kit.diff.label": "Änderung",
  "kit.diff.none": "keine Änderung",
  "kit.diff.more": "… {n} weitere Zeilen",
  "kit.meter.usedOf": "{used} von {limit}",
  "kit.pager.aria": "Seiten",
  "kit.pager.showing": "Zeige {from}–{to}",
  "kit.pager.of": "von",
  "kit.pager.more": "weitere",
  "kit.pager.rows": "Zeilen",
  "kit.pager.rowsAria": "Zeilen pro Seite",
  "kit.pager.previous": "← Zurück",
  "kit.pager.next": "Weiter →",
  "kit.scopeNote": "netzweit",
  "kit.scopeNote.title": "Der in der oberen Leiste gewählte Bereich schränkt dies nicht ein: es zählt das gesamte Netz",
  "kit.table.refreshFailed": "Aktualisierung fehlgeschlagen: {error}",
  "kit.boundary.failed": "Dieser Kasten konnte nicht dargestellt werden: {error}",
  "kit.retry": "Erneut versuchen",
  "kit.loading": "Wird geladen",
  "kit.empty": "Noch nichts vorhanden.",
  "kit.stale.updated": "vor {age} aktualisiert",
  "kit.stale.minutes": "{n} Min.",
  "kit.stale.seconds": "{n} s",
};
