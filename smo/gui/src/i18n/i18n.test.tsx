// @vitest-environment jsdom
/** Tests of the message catalogs and the translator (GUI-6.3): lookup, fallback to English, placeholders, plural forms, the provider and
 * `<html lang>`, and that every translation matches the English catalog (no unknown key, the same placeholders, a plural with its `_other`).
 * Run: `npx vitest run src/i18n`. */
import { afterEach, describe, expect, it } from "vitest";

import { cleanup, mount } from "../testing/dom";
import { de } from "./de";
import { en } from "./en";
import { format, I18nProvider, LANGUAGE_NAMES, LANGUAGES, navKey, translate, useT, type TKey } from ".";

afterEach(cleanup);

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe("translate", () => {
  // A key answers its English text, the German one where there is one, and English where German has none.
  it("looks a key up in the language and falls back to English", () => {
    expect(translate("en", "preferences.title")).toBe("Preferences");
    expect(translate("de", "preferences.title")).toBe("Einstellungen");
    const untranslated = (Object.keys(en) as (keyof typeof en)[]).find((k) => !(k in de));
    if (untranslated) expect(translate("de", untranslated as TKey)).toBe(en[untranslated]);
  });

  // `{name}` is filled from the parameters; one without a value stays visible rather than vanishing.
  it("fills placeholders and leaves a missing one as written", () => {
    expect(translate("de", "preferences.eyebrow", { user: "ana" })).toBe("Angemeldet als ana");
    expect(translate("en", "preferences.eyebrow")).toBe("Signed in as {user}");
    expect(translate("en", "preferences.defaults.browserZone", { other: 1 })).toBe("Browser ({zone})");
  });

  // `count` picks the plural form by the language's rules, falling back to `_other`, then to the other catalog.
  it("picks the plural form by count", () => {
    const enCat = { "rows_one": "{count} row", "rows_other": "{count} rows", "plain": "x" };
    const deCat = { "rows_one": "{count} Zeile", "rows_other": "{count} Zeilen" };
    expect(format(enCat, enCat, "en", "rows", { count: 1 })).toBe("1 row");
    expect(format(enCat, enCat, "en", "rows", { count: 0 })).toBe("0 rows");
    expect(format(deCat, enCat, "de", "rows", { count: 3 })).toBe("3 Zeilen");
    expect(format({}, enCat, "de", "rows", { count: 1 })).toBe("1 row");
    expect(format(enCat, enCat, "en", "plain", { count: 2 })).toBe("plain_other");
    expect(format({}, {}, "en", "missing.key")).toBe("missing.key");
  });

  // A sidebar page's name has a key in both languages.
  it("names every start page through navKey", () => {
    expect(translate("en", navKey("/alarms"))).toBe("Alarms");
    expect(translate("de", navKey("/alarms"))).toBe("Alarme");
  });
});

describe("the catalogs", () => {
  // A translation names only English keys, keeps every placeholder of the English text, and adds none.
  it("hold German to the English keys and placeholders", () => {
    for (const [key, text] of Object.entries(de)) {
      expect(key in en, `unknown key ${key}`).toBe(true);
      expect(placeholders(text ?? ""), key).toEqual(placeholders((en as Record<string, string>)[key]));
    }
  });

  // Every English message has its German text: a converted page is translated whole, never left half English (GUI-6.4).
  it("translate every English message into German", () => {
    expect(Object.keys(en).filter((k) => !(k in de))).toEqual([]);
  });

  // A plural message always has its `_other` form in English, the form every language falls back to.
  it("give every plural message an English _other form", () => {
    for (const key of Object.keys(en)) {
      const base = key.match(/^(.*)_(zero|one|two|few|many)$/)?.[1];
      if (base) expect(`${base}_other` in en, key).toBe(true);
    }
  });

  // Every offered language has its own name, and English is first.
  it("name every language in itself", () => {
    expect(LANGUAGES[0]).toBe("en");
    for (const l of LANGUAGES) expect(LANGUAGE_NAMES[l]).toBeTruthy();
  });
});

describe("I18nProvider", () => {
  function Title() {
    const t = useT();
    return <h1>{t("preferences.title")} ({t.language})</h1>;
  }

  // Below the provider `useT` answers in its language and `<html lang>` says so; without one it answers in English.
  it("answers in its language and sets <html lang>", async () => {
    const { container } = await mount(<I18nProvider language="de"><Title /></I18nProvider>);
    expect(container.textContent).toBe("Einstellungen (de)");
    expect(document.documentElement.lang).toBe("de");
    const plain = await mount(<Title />);
    expect(plain.container.textContent).toBe("Preferences (en)");
  });
});
