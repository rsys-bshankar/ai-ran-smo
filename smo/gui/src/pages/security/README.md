# Account security

Route: `/security`    Design: handoff `Security.dc.html` (PR-SEC-7)

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| security.tiles | sections/StatusTiles.tsx | two-step sign-in on/off, recovery codes left, sign-in method (local / single sign-on) | `/api/me` (session), `GET /api/me/totp` | on change | shared |
| security.authenticator | sections/Authenticator.tsx | set up / confirm the one-time code, make new recovery codes (`components/TotpEnrolment`) | `/api/me/totp/begin`, `/confirm`, `/recovery-codes` | on change | shared |
| security.recovery | sections/RecoveryCodes.tsx | how many recovery codes are left; one masked slot per code, used ones struck through with their time | `GET /api/me/totp` | on change | 1 call (shared key `["bff","totp"]`) |
| security.signins | sections/SignIns.tsx | your newest 20 sign-ins, failed sign-ins and sign-outs, with a warning on a failed one | `GET /api/me/sign-ins?limit=20` | 30 s stale | 1 call |

Every word on the page comes from the catalog (`src/i18n/en/security.ts`, `de/security.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **Recent sign-ins** come from the console's audit log: no IP address or device is recorded beyond what the BFF writes in `detail`.
- The server returns the recovery codes once; afterwards only which slots were used. The codes cannot be downloaded again.
- Moving to a new device: an administrator resets the one-time code (Admin → Users), then the user sets it up again (the BFF has no self re-enrol).
- Password age and "change password" from the mockup: not shown (no password age is served).

## Troubleshooting

- "One-time codes are not set up on this server": the BFF needs `GUI_TOTP_KEY` (or `GUI_TOTP_KEY_FILE`).
- An admin lands here and cannot leave: `GUI_ADMIN_MFA_REQUIRED` is on and the account has no one-time code yet.
