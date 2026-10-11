/** German — the Exports page (see `../en/exports.ts`). */
import type { MessageKey } from "../en";

/** The page's messages in German, by key. */
export const exports: Partial<Record<MessageKey, string>> = {
  "exports.eyebrow": "Konto · Dateien",
  "exports.title": "Exporte",
  "exports.subtitle.before": "CSV-Dateien, im Hintergrund geschrieben aus ",
  "exports.subtitle.decisions": "Entscheidungen",
  "exports.subtitle.after": " und dem Admin-Auditprotokoll; jede wird 24 Stunden nach ihrem Abschluss aufbewahrt",

  "exports.list.title": "Ihre Exporte",
  "exports.list.running_one": "{count} läuft · Aktualisierung alle 2 s",
  "exports.list.running_other": "{count} laufen · Aktualisierung alle 2 s",
  "exports.list.newestFirst": "neueste zuerst",
  "exports.list.allUsers": "Alle Benutzer",
  "exports.list.filterByUser": "Nach Benutzer filtern",
  "exports.list.empty": "Noch kein Export. Verwenden Sie „Exportieren…“ unter Entscheidungen, Alarme oder im Admin-Auditprotokoll.",
  "exports.list.col.what": "Was",
  "exports.list.col.filters": "Filter",
  "exports.list.col.requested": "Angefordert",
  "exports.list.col.state": "Status",
  "exports.list.col.rows": "Zeilen",
  "exports.list.col.size": "Größe",
  "exports.list.col.finished": "Abgeschlossen",
  "exports.list.col.keptUntil": "Aufbewahrt bis",
  "exports.list.noFilters": "keine",
  "exports.list.download": "Herunterladen",
  "exports.list.delete": "Löschen",
  "exports.list.confirmStop": "Diesen Export anhalten und löschen?",
  "exports.list.confirmDelete": "Diesen Export und seine Datei löschen?",
  "exports.list.span": "{from} → {until}",
  "exports.list.firstRecord": "erster Eintrag",
  "exports.list.requestTime": "Zeitpunkt der Anforderung",

  "exports.kind.decisions": "Entscheidungsdatensätze",
  "exports.kind.alarms": "Alarme",
  "exports.kind.audit": "Auditprotokoll",
};
