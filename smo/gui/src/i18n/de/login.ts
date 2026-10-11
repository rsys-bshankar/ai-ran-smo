/** German — the Sign in page (see `../en/login.ts`). */
import type { MessageKey } from "../en";

/** The page's messages in German, by key. */
export const login: Partial<Record<MessageKey, string>> = {
  "login.brand.tagline": "Bedienkonsole · Service Management & Orchestration",
  "login.brand.eyebrow": "O-RAN Non-RT RIC · R1 · O1 · O2",
  "login.brand.headline": "Betreiben Sie das RAN mit rApps, denen Sie vertrauen können.",
  "login.brand.lead": "Jede Änderung, die eine rApp vorschlägt, ist begrenzt, begründet und wird, wenn Sie es wünschen, zur Freigabe durch einen Menschen zurückgehalten, bevor sie das Netz berührt.",
  "login.brand.chip.closedLoop": "Closed-Loop-Automatisierung",
  "login.brand.chip.aiml": "AI/ML-Lebenszyklus",
  "login.brand.chip.intent": "Intent-basiert",
  "login.brand.productOf": "Ein Produkt von ",
  "login.brand.wordmarkAlt": "Radisys",

  "login.form.title": "Anmelden",
  "login.form.lead": "Verwenden Sie Ihr Bedienerkonto, um rApps, AI/ML-Modelle, Alarme und Richtlinien zu verwalten. Jede Aktion wird unter diesem Konto protokolliert.",
  "login.form.sso": "Anmelden mit {provider}",
  "login.form.orLocal": "oder mit einem lokalen Konto",
  "login.form.breakGlass": "Notfall-Anmeldung",
  "login.form.breakGlassOnly": "Nur für Notfallkonten: ein Passwort und ein Einmalcode.",
  "login.form.username": "Benutzername",
  "login.form.password": "Passwort",
  "login.form.signingIn": "Anmeldung läuft…",
  "login.form.submit": "Anmelden",
  "login.form.codeNote": "Hat Ihr Konto einen Einmalcode, folgt auf das Passwort der Code aus Ihrer Authenticator-App. Gerät verloren? Verwenden Sie einen Wiederherstellungscode.",
  "login.form.breakGlassNote": "Die Notfall-Anmeldung bleibt verfügbar, wenn Single Sign-on ausgefallen ist. Wiederholte Fehlversuche sperren das Konto.",
};
