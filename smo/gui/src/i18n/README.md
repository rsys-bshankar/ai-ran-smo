# The console's languages (`GUI-6.3`, `GUI-6.4`)

The words of the console come from message catalogs here, in the language the user picks on **Account → Preferences** (`language`, kept by the
BFF with the other preferences). English is the source language; German is the second. A page not translated yet still shows English.

| File | What it is |
|---|---|
| `index.tsx` | `useT()` (the translator of the nearest provider; English outside one), `I18nProvider` (set by `shell/ThemeProvider.tsx` from the shown preferences; it also sets `<html lang>`), `translate`, `format`, `LANGUAGES`, `LANGUAGE_NAMES`, `navKey` |
| `en/` | The English catalog: `common.ts` (shared: the page names, and the sentences of `lib/domain.ts` and `lib/mfa.ts`), `lib.ts` (the rest of what the shared helpers answer: `lib/flows.ts`, `lib/domain.ts`, `lib/lifecycle.ts`, `lib/mfa.ts`, `lib/oidc.ts`, `data/exports.ts`), one file per page (`preferences.ts`) and one per shared directory (`shell.ts`, `kit.ts`, `components.ts`), merged in `en/index.ts`. Its keys are the type `MessageKey` |
| `de/` | The German catalog, the same layout, typed `Partial<Record<MessageKey, string>>`: a key it lacks falls back to English |
| `literals-baseline.json` | How many words each area still has written in the code (`GUI-6.4`), held by `literals.test.ts` |

## Writing a message

```tsx
const t = useT();
<Card title={t("alarms.list.title")} sub={t("alarms.list.sub", { count: total })} />
```

- A key is `<area>.<what>`, the area the page's directory name (`alarms.`, `kpis.`), `common.` for what pages share.
- A placeholder is `{name}`, filled from the parameters; one with no value stays visible, so a missed parameter shows on screen and in tests.
- A message that depends on a number has `<key>_one` and `<key>_other` (and other forms a language needs, `Intl.PluralRules`); the code names the
  base key and passes `count`.
- Values from the network (element names, state names such as `RUNNING`, ids, units) are not translated; nor are each language's own name.
- A translation must keep exactly the English placeholders (`i18n.test.tsx` fails otherwise).

## Converting a page (`GUI-6.4`)

1. `node scripts/i18n-literals.mjs pages/<page>` lists the page's literals with file and line.
2. Move each into `en/<page>.ts` (and add the file to `en/index.ts`), use `t(...)` in the component, and add the German text to `de/<page>.ts`.
3. `node scripts/i18n-literals.mjs --write` lowers the baseline; the page should reach 0. `npx vitest run` holds every area to its count, so none can
   grow again, and a page at 0 stays there.

The check (`scripts/i18n-literals.mjs`) reads the syntax tree: JSX text, the text attributes and properties a person reads (`title`, `label`,
`placeholder`, `aria-label`, `header`, `success`, ...), and strings given as JSX children. A word kept in a constant and shown later is not seen, so
a reviewer still reads a converted page in German once.
