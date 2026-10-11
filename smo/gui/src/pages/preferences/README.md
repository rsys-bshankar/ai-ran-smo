# Preferences

Route: `/preferences`    Design: handoff `Preferences.dc.html` (BRIEF §4d)

## Sections

| id | file | what it shows | API | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| preferences.language | sections/LanguagePicker.tsx | English / Deutsch (GUI-6.3); previews at once | — (draft) | — | 0 |
| preferences.theme | sections/ThemePicker.tsx | dark / light / system | — (draft) | — | 0 |
| preferences.accent | sections/AccentPicker.tsx | five accents | — (draft) | — | 0 |
| preferences.size | sections/TextSize.tsx | s / m / l / xl root text size | — (draft) | — | 0 |
| preferences.defaults | sections/DisplayDefaults.tsx | start page, rows per page, time zone, clock, motion, sound | — (draft) | — | 0 |
| preferences.preview | sections/LivePreview.tsx | sample tiles, chips, buttons | — | — | 0 |

The page reads the saved preferences from `shell/ThemeProvider.tsx` (`GET /api/me/preferences`, one call per session, shared by every page) and
writes them with `PUT /api/me/preferences` (gui-bff/app/preferences.py). Every word on the page comes from the catalog (`src/i18n/en/preferences.ts`,
`de/preferences.ts`); the page is the first one translated (GUI-6.3) and its literal count is 0.

## Known limits

- The alarm sound plays only while the pushed summary stream is open (top bar "Live · pushed"), when the critical alarm count rises; a browser
  that blocks audio until the page was clicked stays silent until then.
- "Network local" time zone from the mockup is not offered: no backend serves the network's time zone.

## Troubleshooting

- A change shows here but not on other pages: it is a preview until **Save preferences**.
- The first paint after sign-in is in the old theme: the browser copy (`localStorage["smo.prefs"]`) was cleared; the server copy applies a moment later.
- Save answers 422: a value outside its set (gui-bff validates every field).
