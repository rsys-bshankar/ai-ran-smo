# Sign in

Route: `/login`    Design: handoff `Login.dc.html` (BRIEF §4 "Sign in")

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| login.brand | sections/BrandPanel.tsx | product name ("Operator Console"), headline, sector-ring graphic, "A product of Radisys" wordmark chip | — (static, `/brand/*.png`) | — | 0 |
| login.form | sections/SignInForm.tsx | SSO button first (when OIDC is on), local account form, one-time-code note; the code step (`components/LoginCodeStep`); break-glass link in oidc mode | `GET /api/auth/config`; `POST /api/login`, `POST /api/login/totp` through `auth/AuthContext` | — | 1 call |

Every word on the page comes from the catalog, `src/i18n/en/login.ts` and `de/login.ts` (GUI-6.4); the product name "AI-RAN SMO" is not translated.

## Known limits

- "Keep me signed in on this device" from the mockup is not offered: the BFF has no such session option.
- The browser checks (`scripts/gui_e2e.py`, `gui_smoke.py`, `gui_oidc_e2e.py`) find the form by the labels **Username** and **Password**, the
  button **Sign in**, the link **Sign in with <provider>** and the text **Operator Console**: keep those names.

## Troubleshooting

- Only the SSO button shows: the BFF runs with `GUI_LOGIN_MODE=oidc`; a break-glass account uses the "Break-glass sign-in" link.
- An error after the provider's redirect: the sentence comes from `lib/oidc.ts` for the `oidc_error` reason code; the provider's own text is never shown.
- Back on the password step after entering a code: the challenge expired or was spent, or the account is locked (429).
