/** The console's words in the user's language (GUI-6.3; README.md beside this file).
 *
 * Every message is a key of the English catalog (`en/`, the source language); another language (`de/`) holds what it has translated, and a key
 * it lacks is shown in English, so a page can be translated one message at a time and never shows a key. A message may name parameters,
 * `{user}`, filled from `t(key, { user })`; a message that depends on a number has the forms `<key>_one` and `<key>_other` (and any other
 * plural category the language has: `Intl.PluralRules`), chosen by the `count` parameter. No library: the catalogs are plain objects, typed by
 * the English one, so a misspelt key does not compile.
 *
 * The language is the user's `language` preference (data/preferences.ts), applied by `shell/ThemeProvider.tsx` through `I18nProvider`
 * (which also sets `<html lang>`); outside a provider (a component test) `useT` answers in English. */
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";

import { de } from "./de";
import { en, type MessageKey } from "./en";

export type { MessageKey } from "./en";

/** The languages the console offers, in the order the Preferences page lists them. */
export const LANGUAGES = ["en", "de"] as const;
export type Language = (typeof LANGUAGES)[number];

/** Each language's own name for itself (what the language picker shows, in every language). */
export const LANGUAGE_NAMES: Record<Language, string> = { en: "English", de: "Deutsch" };

const CATALOGS: Record<Language, Partial<Record<MessageKey, string>>> = { en, de };

/** A key as code names it: a plural message by its base (`alarms.open` for `alarms.open_one` / `alarms.open_other`). */
export type TKey = MessageKey extends infer K ? (K extends `${infer B}_${"zero" | "one" | "two" | "few" | "many" | "other"}` ? B : K) : never;

/** The values a message's `{name}` placeholders take; `count` also picks the plural form. */
export type Params = Record<string, string | number>;

const plurals = new Map<string, Intl.PluralRules>();

/** The message `id` of `catalog` (else of `fallback`, else the id itself) in language `lang`, its plural form picked by `params.count` and its
 * placeholders filled from `params` (a placeholder with no value stays as written, so a mistake shows). `translate` is this over the real catalogs. */
export function format(catalog: Record<string, string | undefined>, fallback: Record<string, string | undefined>, lang: string, id: string, params?: Params): string {
  let key = id;
  if (params && typeof params.count === "number") {
    const rules = plurals.get(lang) ?? new Intl.PluralRules(lang);
    plurals.set(lang, rules);
    const form = `${id}_${rules.select(params.count)}`;
    key = form in fallback ? form : `${id}_other`;
  }
  const text = catalog[key] ?? fallback[key] ?? key;
  return params ? text.replace(/\{(\w+)\}/g, (whole, name: string) => (name in params ? String(params[name]) : whole)) : text;
}

/** The message `key` in `lang` (English where `lang` has no translation of it). */
export function translate(lang: Language, key: TKey, params?: Params): string {
  return format(CATALOGS[lang] ?? en, en, lang, key, params);
}

/** What `useT` hands a component: `t(key, params?)`, and the language it answers in. */
export interface Translator {
  (key: TKey, params?: Params): string;
  language: Language;
}

function translator(lang: Language): Translator {
  return Object.assign((key: TKey, params?: Params) => translate(lang, key, params), { language: lang });
}

const Ctx = createContext<Translator>(translator("en"));

/** Answers in `language` below it, and sets `<html lang>` to it (screen readers pick their voice by it). */
export function I18nProvider({ language, children }: { language: Language; children: ReactNode }) {
  const t = useMemo(() => translator(language), [language]);
  useEffect(() => { document.documentElement.lang = language; }, [language]);
  return <Ctx.Provider value={t}>{children}</Ctx.Provider>;
}

/** The translator of the nearest `I18nProvider` (English outside one). */
export function useT(): Translator {
  return useContext(Ctx);
}

/** The catalog key of a sidebar page's name (`nav./alarms`), for a component that lists pages by their path. */
export function navKey(to: string): TKey {
  return `nav.${to}` as TKey;
}
