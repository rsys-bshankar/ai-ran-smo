/** German — the messages every page shares (see `../en/common.ts`). */
import type { MessageKey } from "../en";

export const common: Partial<Record<MessageKey, string>> = {
  "nav./": "Übersicht",
  "nav./flows": "Lebenszyklus-Abläufe",
  "nav./rapps": "rApps",
  "nav./approvals": "Freigaben",
  "nav./decisions": "Entscheidungen",
  "nav./safeguards": "Schutzmechanismen",
  "nav./aiml": "KI/ML",
  "nav./policy": "Intents",
  "nav./alarms": "Alarme",
  "nav./kpis": "KPIs & Qualitätssicherung",
  "nav./topology": "RAN-Topologie",
  "nav./configuration": "Konfiguration",
  "nav./software": "Software",
  "nav./infrastructure": "Infrastruktur",
  "nav./data": "Daten & Bereitstellung",
  "nav./preferences": "Einstellungen",
  "nav./security": "Kontosicherheit",
  "nav./exports": "Exporte",
  "nav./admin": "Administration",

  "domain.disposition.DIRECT": "Sofort geschrieben: die Änderungen dieser rApp werden nicht zur Freigabe zurückgehalten",
  "domain.disposition.APPROVED": "Geschrieben, nachdem eine Person sie freigegeben hat",
  "domain.disposition.ROLLBACK": "Das Rückgängigmachen eines früheren Auftrags",
  "domain.disposition.REJECTED": "Eine Person (oder die Zeitüberschreitungsregel) hat sie abgelehnt: nichts wurde geschrieben",
  "domain.disposition.EXPIRED": "Niemand hat rechtzeitig entschieden: nichts wurde geschrieben",
  "domain.disposition.REFUSED": "Freigegeben, dann von einem Schutzmechanismus oder einer Prüfung abgewiesen: nichts wurde geschrieben",
  "domain.integrity.VERIFIED": "Der Datensatz stimmt noch mit dem Hash überein, der in die Audit-Kette geschrieben wurde",
  "domain.integrity.UNCHAINED": "Noch nicht in die Audit-Kette geschrieben (kurz nach dem Auftrag; der Worker versucht es erneut)",
  "domain.integrity.MISMATCH": "Der Datensatz oder seine Audit-Zeile wurde nach dem Schreiben geändert",

  "mfa.recoveryLeft_one": "{count} Wiederherstellungscode übrig.",
  "mfa.recoveryLeft_other": "{count} Wiederherstellungscodes übrig.",
  "mfa.recoveryLeft.none": "Sie haben keine Wiederherstellungscodes mehr: erzeugen Sie auf der Seite Kontosicherheit neue.",
};
