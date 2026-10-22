# Operator console (gui): lifecycle

The console is a single-page app (React, React Query, React Router) served by an unprivileged nginx, which also forwards `/api/...` to the GUI backend.
The browser talks to nothing else: every read and every action goes to the BFF ([`../../gui-bff/docs/lifecycle.md`](../../gui-bff/docs/lifecycle.md)),
which checks it and calls the SMO. Pages, roles and security: [`../README.md`](../README.md). This document follows one browser tab from loading to
closing. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Loading the console | a person opening the console's URL | the shell drawn for the signed-in user, or the sign-in page |
| b | Signing in and out | a person at the sign-in page or in the sidebar | the session in the BFF's cookies, every cached read of the last user dropped |
| c | Reading data: polling and the event stream | every open page, the shell | tables and counts kept fresh while the tab is visible |
| d | Actions through the BFF | a button of a page, or of a rApp's declared page | a toast, and only the reads the action can have changed refetched |
| e | Errors and a lost session | any failed call, a crashed section | one readable line per error, a retry where it can help, the sign-in page on a 401 |

**How to read the diagrams.** As in [call flow 01](../../docs/call-flows/01-rapp-onboarding-to-deployment.md): an arrow is one call with the inputs
that matter, a box on the receiver says what it does with them, an `alt` block is the outcome (success first, then each failure and what the page
shows). *nginx* is the console's own container (`nginx.conf`); *BFF* stands for the GUI backend and everything behind it. Every `/api/...` call is
same-origin with `credentials: same-origin`, so the session cookie rides along, and every POST, PUT, PATCH and DELETE echoes the readable `smo_csrf`
cookie as `X-CSRF-Token`.

## a — Loading the console

```mermaid
sequenceDiagram
    participant Br as Browser
    participant N as nginx
    participant App as SPA
    participant B as BFF

    Br->>N: GET / (any path)
    N-->>Br: index.html (Cache-Control no-cache, the CSP and security headers)
    Br->>N: GET /theme-boot.js (no-cache), /assets/... (hashed names, cached a year)
    Note over Br: theme-boot.js puts the browser copy of the preferences (localStorage smo.prefs)<br/>on html before React runs: theme, accent, size, motion, language
    Br->>App: main.tsx mounts the query client, toasts, AuthProvider, ThemeProvider, router
    App->>B: GET /api/me
    alt 401
        Note over App: signed out: RequireAuth sends every route but /login to /login, remembering where it was going
    else 200 {username, role, csrfToken, scope, mfa state}
        App->>B: GET /api/permissions and GET /api/me/preferences (neither while an admin must enrol a code)
        Note over App: preferences applied and copied to localStorage,<br/>a local admin who must enrol is sent to /security,<br/>a route above the role (/exports operator, /admin admin) sent to /,<br/>a fresh visit to / opens the start page chosen in Preferences
        App->>Br: the shell (sidebar, top bar, scope picker, live events) and the page,<br/>a page other than Dashboard and sign-in loaded as its own chunk on first open
    end
```

The console has no build-time setting of its own: no API address is baked in, because every call is a relative `/api/...` path. Where `/api/` goes is
nginx's (`gui-bff:8000`, read timeout 60 s, body up to 50 MiB for package and artifact uploads, buffering off for `/api/events`); in development the
Vite server forwards it to `GUI_BFF_URL` (default `http://localhost:8090`). The CSP allows scripts, styles, fonts and connections from `self` only,
which is why the theme script is a file and the fonts are self-hosted. `/api/permissions` is the BFF's own rule table: the SPA evaluates the same
rules to decide which buttons to show, and the BFF checks every call again.

## b — Signing in and out

```mermaid
sequenceDiagram
    actor P as Person
    participant App as SPA
    participant B as BFF
    participant IdP as OIDC provider

    App->>B: GET /api/auth/config
    B-->>App: {localLogin, loginMode, breakGlass, oidc {enabled, providerName, loginUrl}}
    Note over App: the password form unless it is off, a break-glass link under GUI_LOGIN_MODE=oidc,<br/>Sign in with provider when OIDC is on, the oidc_error of the URL as the page's own words
    alt password
        P->>App: user name and password
        App->>B: POST /api/login {username, password}
        alt a challenge {mfaRequired, challenge, expiresIn}
            P->>App: the one-time code or a recovery code
            App->>B: POST /api/login/totp {challenge, code}
        end
        B-->>App: 200 {username, role, csrfToken, ...} and the cookies, or 401, 403, 429, 503 shown on the form
        Note over App: me set in place, every other cached query removed,<br/>then the page it was going to, else the Dashboard
    else single sign-on
        App->>B: full-page navigation to /api/oidc/login
        B-->>App: 302 to the provider
        P->>IdP: signs in
        IdP-->>App: back through /api/oidc/callback, 303 to / with the cookies, or /login?oidc_error=code
    end
    P->>App: Sign out
    App->>B: POST /api/logout
    Note over App: me cleared and every cached read removed whatever the answer,<br/>a failure other than 401 told in a toast (the session then ends at expiry)
    opt endSessionUrl (an OIDC user)
        App->>IdP: window.location to the provider's end-session page
    end
```

A 401 on the two sign-in routes is a form error; anywhere else it means the session ended (e). Removing the cached queries at sign-in and sign-out
means the next person at the same browser never sees the last one's data. The password change dialog (`POST /api/me/password`) and the Account
security page (`/api/me/totp...`, `/api/me/sign-ins`) are ordinary BFF calls.

## c — Reading data: polling and the event stream

```mermaid
sequenceDiagram
    participant Pg as Page
    participant Q as Query cache
    participant L as LiveEvents (shell)
    participant B as BFF

    Pg->>Q: useSmo(path, query) keyed [smo, path, query], scope region and site_cluster added where the route takes them
    Q->>B: GET /api/smo/{module}/{path}, a page envelope unwrapped to its items
    Note over Q: refetched every 15 s (lists), 5 s (alarms), 10 s (module status),<br/>on window focus, never while the tab is hidden, fresh for 2 s
    Pg->>Q: useSummary(page): GET /api/summary/{page} every 15 s (60 s while the stream is open)
    L->>B: EventSource /api/events?topics=summary:nav, the visible page, summary:attention on the Dashboard
    alt open
        B-->>L: event summary or attention (full counts first, then only changes), ping every 15 s
        Note over L: counts written into the summary cache, the lists whose counts changed refetched,<br/>a beep when critical alarms rose and the alarm sound is on, top bar Live pushed
    else refused (a 4xx) or dropped
        Note over L: reopened after 1 s, doubling to 60 s, the summary polls carry on meanwhile,<br/>a page topic refused twice before opening: nav only until another page
    end
    Note over L: hidden tab: stream closed, reopened when shown,<br/>a page or scope change reopens it, none while an admin must enrol a code
```

The polls are the floor and the stream the fast path: a tile reads a summary count from the BFF (5 s cache shared by every user) rather than a list,
and while the stream is open the counts arrive pushed and the summary is read only once a minute as a safety net. The BFF ends a stream after 30 minutes
and the browser reconnects on its own. The scope picker (`?region=`, `?site_cluster=`) narrows every route that takes it and scopes every stream topic.

## d — Actions through the BFF

```mermaid
sequenceDiagram
    actor P as Person
    participant Pg as Page
    participant Q as Query cache
    participant B as BFF

    P->>Pg: a button (shown only if can(method, path) allows it for the role)
    alt an SMO module action
        Pg->>B: POST/PUT/PATCH/DELETE /api/smo/{module}/{path}, JSON or multipart, X-CSRF-Token
    else a rApp's declared action
        Pg->>B: /api/rapps/{instance}/operator/{path}, X-Action-Id, X-CSRF-Token
    end
    alt 2xx
        B-->>Pg: the module's answer, X-Correlation-ID
        Pg->>Q: success toast, refetch: the module's reads, those of the modules a call there changes too<br/>(rapp-mgmt also onboarding, nfo, sme, dme, so-smos the AI/ML and infrastructure ones), the summary counts
    else 4xx or 5xx
        B-->>Pg: problem {title, detail}
        Pg->>P: error toast "METHOD path failed" with the readable line
    end
```

The rApp actions refresh the rApp's declared reads and those of rApp Management and RAN NF OAM; a `502 UPSTREAM_UNAVAILABLE` from R1 adds a hint that
the rApp's operator API is not reachable. What the console forces into a request (who acknowledged, who decided) is set by the BFF, not by the page.

## e — Errors and a lost session

```mermaid
sequenceDiagram
    participant Pg as Page or section
    participant C as api client
    participant A as AuthProvider
    participant B as BFF

    Pg->>C: api(path) or smo(path)
    C->>B: fetch /api/...
    alt 2xx
        B-->>C: JSON (text or nothing when not JSON)
    else 401 outside /login and /login/totp
        C->>A: window event smo:unauthorized
        Note over A: me set to null: the router shows /login, the page it was on remembered
    else any other failure
        Note over C: ApiError(status, title, detail) from the BFF's problem, a module's<br/>{detail}, FastAPI's validation list or an OAuth2 error
    end
    alt a read
        Note over Pg: 4xx not retried, 5xx and network errors retried twice,<br/>then the box shows the error with Retry (loading, empty, partial and stale states too)
    else a section crashes while drawing
        Note over Pg: its SectionBoundary shows the error and Retry, the rest of the page works
    end
```

A 403 `MFA_ENROLMENT_REQUIRED` cannot happen in normal use, because the route guard sends such an admin to Account security first; a 403 `FORBIDDEN`
on an action means the SPA's copy of the rules and the BFF disagreed, and the BFF wins. A module that does not answer shows as a partial count
("—", with the module named) rather than a failed page.

## Configuration it reads

| Setting | Default | What it changes |
|---|---|---|
| nginx `location /api/` | `proxy_pass http://gui-bff:8000`, read timeout 60 s, body 50 MiB | Where every call goes, how long one may take, the largest upload |
| nginx `location = /api/events` | buffering off, read timeout 3600 s | That pushed events reach the browser at once |
| `security-headers.conf` | CSP `default-src 'self'` and the rest | What the page may load and connect to |
| `GUI_BFF_URL` (Vite dev and preview only) | `http://localhost:8090` | The BFF a developer's console talks to |
| Polling cadences (`POLL` in `src/api/hooks.ts`) | lists 15 s, alarms 5 s, status 10 s, summary 15 s, inventory 60 s | How fresh a page is without the stream |
| `localStorage` `smo.prefs` | the BFF's defaults | The first paint's theme, size and language before `/api/me/preferences` answers |

## Decisions this module depends on

- **The browser talks only to the BFF, same origin** (gui README "Security"): no CORS, no token in the page, the session in an HttpOnly cookie with
  double-submit CSRF.
- **The SPA hides, the BFF decides** (`src/auth/permissions.fixture.json` is the BFF's table; `test_rbac.py` fails on drift).
- **Counts, not lists, and pushed** (SCALE.md P7, the stream; P8, targeted invalidation; P10, the explicit loading, empty, error, partial and stale
  states).
- **A second factor before anything else for a local admin** (PR-SEC-7.8); **single sign-on with RP-initiated logout** (PR-SEC-6).
- **A sign-out that the server did not confirm still signs out here** (GUI-10.7); **the scope picker** (GUI-9.3).
