# SMO Operator GUI

A React + TypeScript single-page console for operating the SMO modules and
every rApp, plus its backend-for-frontend (`../gui-bff`). It has **one rApps entry**: a
directory of all rApps, and for each a page its package declares and a generic renderer draws.

```
Browser ──► gui (nginx :3000) ──/api──► gui-bff ──Bearer (SME-issued)──► R1 Termination ──► modules
```

- The browser talks **only** to its own origin. `/api` is reverse-proxied
  to the BFF; it never calls R1 Termination or a module port directly (no
  CORS anywhere, and no way around the role checks).
- The **BFF** holds GUI users and roles, issues the session JWT, checks
  every call against its permission table (`../gui-bff/app/rbac.py`), and
  forwards allowed calls to R1 Termination. It authenticates to R1 the way
  an rApp does: the token endpoint advertised by R1's `/bootstrap`, a CAPIF
  invoker identity onboarded once at SME, then `client_credentials`.
- Module routes stay the source of truth: the GUI drives the documented
  FastAPI endpoints (`../docs/openapi/<module>.json`) and doesn't
  reimplement any lifecycle logic.

Screenshots of the pages, tabs and lifecycle flows: [Screenshots](#screenshots).

## Look, structure and scale (the "Signal" redesign, `PR-GUI-9`)

The console follows the design hand-off kept in [`docs/redesign/`](docs/redesign/README.md): a dark-first theme with a light theme, five
accent colours and four text sizes, chosen per user on **Account → Preferences** and stored by the BFF (`/api/me/preferences`). Code layout:

| Folder | What lives there |
|---|---|
| `src/shell/` | The frame: `Layout`, `Sidebar` (grouped navigation, count badges, pinned rApps, user card), `TopBar` (breadcrumb, ⌘K search: page jumps plus the BFF's `/api/search`, live chip, notifications, preferences, help), `LiveEvents` (the summary stream), `ThemeProvider`, `nav.ts` (the one navigation table) |
| `src/kit/` | Shared primitives, one per file: `Kpi`, `Meter`, `Segmented`, `Badge`, `Callout`, `Diff`, `Timeline`/`Steps`, `states` (skeleton, empty, error with retry, stale), `SectionBoundary`, `ServerTable` + `Pager`, `icons` |
| `src/data/` | `summary.ts` (true counts from `GET /api/summary/{page}`, and the Dashboard's attention groups from `GET /api/summary/attention`), `scope.ts` (the global scope: `useScope()`, the scopable routes), `exports.ts` (the export jobs), `keys.ts` (which reads an action refreshes), `events.ts` (the pushed summaries), `preferences.ts` |
| `src/pages/<page>/` | One folder per page: `index.tsx` (layout only), `sections/` (one box each, wrapped in a `SectionBoundary` so a crash stays in its box), `data/queries.ts` (the page's API paths and polling), `README.md` (the page's maintenance sheet: sections, calls, known limits, troubleshooting), `__tests__/` |

Rules every page keeps (SCALE.md in the hand-off): a list is paged by the backend (`kit/ServerTable`: "Showing 1–50 of N", 25/50/100
rows), never cut at the backend's default 100 rows and never counted in the browser; a tile, badge or meter reads the summary counts; a
value the backend does not serve shows "—" and the page README says so; only the visible tab loads; every page but the Dashboard and the
sign-in is loaded when first opened.

## Run it

With the whole stack:

```bash
cd smo
scripts/init_secrets.sh   # once: the database password (secrets/db_password), no default
export GUI_ADMIN_PASSWORD='choose-one' GUI_OPERATOR_PASSWORD='…' GUI_VIEWER_PASSWORD='…' GUI_JWT_SECRET="$(openssl rand -base64 48)"
docker compose up --build
# open http://localhost:3000
```

| Variable (gui-bff) | Default | Meaning |
|---|---|---|
| `R1_URL` | `http://r1-termination:8000` | The only southbound the BFF talks to |
| `SME_URL` | discovered via R1 `/bootstrap` | Override the SME token endpoint base |
| `GUI_ADMIN_PASSWORD` | random, written to `GUI_INITIAL_PASSWORD_FILE` | Seeds user `admin` on first boot only |
| `GUI_INITIAL_PASSWORD_FILE` | `/data/initial-admin-password` in compose | Where a generated admin password goes (mode 0600, never logged): `docker compose exec gui-bff cat /data/initial-admin-password`; delete it after changing the password |
| `GUI_OPERATOR_PASSWORD` / `GUI_VIEWER_PASSWORD` | unset → user not created | Seed users `operator` / `viewer` |
| `GUI_JWT_SECRET` | unset: generated once and stored in the BFF database, so sessions survive restarts and are shared by every instance of that database | HS256 session signing key |
| `GUI_COOKIE_SECURE` | `true` | Set `false` only for plain-http access by a non-localhost name |
| `GUI_DATABASE_URL` | `sqlite:////data/gui-bff.db` (compose volume) | Users, audit log, the BFF's SME credential |
| `GUI_SESSION_TTL_SECONDS` | `28800` | Session lifetime |
| `GUI_OIDC_ENABLED` and the other `GUI_OIDC_*` | `false` | OIDC sign-in next to the password form (PR-SEC-6): issuer, client id, `GUI_OIDC_CLIENT_SECRET[_FILE]`, redirect URI, scopes, the groups claim, `GUI_OIDC_GROUP_ROLE_MAP` (`group=role,...`), `GUI_OIDC_DEFAULT_ROLE`, `GUI_OIDC_PROVIDER_NAME`; see [`../gui-bff/README.md`](../gui-bff/README.md) section 2.9 |
| `GUI_LOCAL_LOGIN_ENABLED` | `true` | `false` hides the password form and refuses `/api/login` and `/api/token` (needs OIDC on); leave `true` for the break-glass admin |
| `GUI_LOGIN_MODE` | `both` | `oidc`: the sign-in page shows only "Sign in with <provider>" (a small "Break-glass sign-in" link opens the form for accounts an admin flagged break-glass); `local`: OIDC is not offered even when configured (PR-SEC-7.6) |
| `GUI_TOTP_KEY` / `GUI_TOTP_KEY_FILE`, `GUI_TOTP_ISSUER` | unset | The key that makes one-time codes (authenticator app) available to local accounts: Account security then offers "Set up a one-time code"; without it the page says so (PR-SEC-7.1) |
| `GUI_ADMIN_MFA_REQUIRED` | `false` | `true`: a local admin without a one-time code sees only Account security until they have enrolled one (PR-SEC-7.8) |

No password ever lives in git: seed users are hashed (salted scrypt) from
the environment on the first boot, and never touched again after that.
Manage them afterwards under **Admin → Users & roles**.

Development, against a BFF on `:8090` (itself pointed at a running R1):

```bash
cd smo/gui-bff && R1_URL=http://localhost:8080 GUI_ADMIN_PASSWORD=admin-pass-1 GUI_COOKIE_SECURE=true \
  PYTHONPATH=. uvicorn app.main:app --port 8090
cd smo/gui && npm install && npm run dev     # http://localhost:5173, /api proxied to :8090 (GUI_BFF_URL overrides)
```

Tests:

```bash
cd smo/gui-bff && PYTHONPATH=. python -m pytest tests -q     # auth, CSRF, RBAC, proxy, health, admin/audit
cd smo/gui && npm test && npm run typecheck && npm run build   # Vitest: role gating, API helpers, domain logic, the sign-in, one-time-code and enrolment pages, the renderer of a declared page, the rApp directory, the approval inbox, the decision list and record, the approval policy dialog, and Configuration's element onboarding and the Software page's campaigns (their forms, the actions each state allows, the failure-notice watchers, axe-core on them)
```

`src/auth/permissions.fixture.json` is a snapshot of the BFF's permission
table: the SPA's Vitest suite evaluates it (so the Python regexes provably
behave the same in JS), and the BFF's `test_rbac.py` fails if it drifts.
Regenerate with `cd smo/gui-bff && PYTHONPATH=. python scripts/export_permissions.py`.

## Roles

The BFF is authoritative. The SPA reads the same table (`GET /api/permissions`)
to hide what a role can't do, and the BFF re-checks every proxied call anyway.
A route missing from the table is refused for everyone, admin included (SME
token/registration APIs, DME producer registration, NFO Instantiate, usage
registrations, heartbeats: machine-to-machine only).

| | Viewer | Operator | Admin |
|---|:-:|:-:|:-:|
| Every read: status, lists, details, alarms, KPIs (feature groups excepted: they carry datalake tokens) | ✓ | ✓ | ✓ |
| Lifecycle: onboard/prime/deprime/deprecate packages; create, configure, upgrade, recover, bootstrap instances; register, edit, train, advance, deploy models, inference, training-metrics writeback, feature groups; ack/clear alarms; CM writes, PM subscriptions, SW jobs; A1 policies and policy-status subscriptions, intents, analytics subscriptions; DME consumer data jobs and type subscriptions; SME event subscriptions; FOCOM inventory subscriptions; SO orders; SA monitor evaluate / remediate / escalate; NFO heal/scale | | ✓ | ✓ |
| Hard deletes and teardown: delete packages, terminate/delete instances, deprecate/delete models, delete intents, terminate NF deployments, provision/deprovision O-Cloud resources | | | ✓ |
| Registry administration: SME providers, published service APIs, invoker onboarding (the one-time secret is shown once) and trusted-invoker security contexts; RMIH registration (framework identities only) | | | ✓ |
| Acting as another party, to exercise a flow: DME producer types and offers, RAN Analytics producers and reports, intent fulfilment reports, package usage registrations, O1 heartbeats, and test alarms, rApp perf/faults and MLMF reports; GUI users and the audit log | | | ✓ |

The BFF also pins identity-bearing parameters rather than trusting the
browser: `ack_user_id` / `clear_user_id` are the GUI user; SA SMOS's
`requester_is_admin` follows the GUI role; a CM write's `requestedBy` is the
GUI user and its MSAC tier (which `entire-RAN` scope requires) is granted to
admins only; the `requestedBy` of an onboarding apply, a campaign start and a campaign halt, continue, abort or roll back is the GUI user too (the pages send none); onboarding templates and lifecycle subscriptions are an admin's, applying a template and driving a campaign an operator's; Intent Service intents carry RMIO `smo-gui` (so the GUI can change
the admin state of exactly the intents it created).

## Security

- Session JWT in an `HttpOnly; Secure; SameSite=Strict` cookie scoped to
  `/api`. Unsafe methods also need `X-CSRF-Token` matching the claim inside
  the JWT (double submit). Scripts can use `POST /api/token` (OAuth2 password
  grant) and send `Authorization: Bearer`.
- Optional OIDC sign-in (PR-SEC-6, `GUI_OIDC_ENABLED`): the sign-in page then also shows "Sign in with <provider>". The BFF runs the authorization-code flow with
  PKCE, validates the ID token (signature from the provider's JWKS, issuer, audience, expiry, nonce), maps a groups claim to viewer/operator/admin through
  `GUI_OIDC_GROUP_ROLE_MAP` (a user in no mapped group is refused), creates the user `oidc:<subject>` on first sign-in with no password, and then issues the
  same cookie session as a password login. Multi-factor is the provider's. Signing out also offers the provider's end-session page. The local admin stays
  as the break-glass account. An error from the provider is shown as a fixed sentence per reason, never the provider's own text.
- One-time codes for local accounts (PR-SEC-7, [`../gui-bff/README.md`](../gui-bff/README.md) section 2.10). After the password, an account with a code is asked for the 6-digit code of its
  authenticator app, or one recovery code (the sign-in page's second step; a wrong code counts towards the same lockout as a wrong password). **Account security** (sidebar, every role) sets
  the code up: it shows the setup key to type into an app (there is no QR image) and an `otpauth://` link, activates it with the first valid code, then shows ten recovery codes **once**
  ("I have saved them" closes the page; a copy button helps); an enrolled account sees how many recovery codes are left and can make new ones with a current code. After a recovery code
  the sign-in says how many are left. Admin, Users shows each user's code (enrolled / not set up), a Break-glass checkbox, and the buttons "Revoke sessions" and "Reset one-time code"
  (a lost device). With `GUI_ADMIN_MFA_REQUIRED` an admin without a code is sent to Account security and the sidebar shows nothing else until it is done.
- Roles are read from the user table on every request: a demotion applies
  immediately, and a password reset or deactivation revokes existing sessions.
- 5 failed logins lock an account for 5 minutes. Unknown users cost the same
  hash work as known ones.
- nginx: strict CSP (`script-src 'self'`, no inline script), `frame-ancestors
  'none'`, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`. The BFF: `default-src 'none'` CSP, `no-store`.
- The proxy forwards only `Content-Type`/`Accept`. Browser cookies and
  credentials never reach R1, hop-by-hop headers are stripped both ways, and
  an upstream `Set-Cookie` never reaches the browser.
- Append-only audit log (**Admin → Audit log**): every mutating proxied call
  (allowed or denied, with its status), plus sign-ins and user administration.
- Only `gui` (:3000) and R1 Termination (:8080, for rApps, unchanged) publish
  ports. `gui-bff` publishes nothing, and nothing else is exposed.

## Pages and the R1 paths they use

| Page | Module paths (all via `/api/smo/…` → R1) |
|---|---|
| **Lifecycle flows** | Nine `docs/call-flows` journeys, each a live step timeline for a chosen package / model / config job / monitor / instance / analytics type / intent / order, with the next action on the current step (`lib/flows.ts` holds the step logic). Reads everything the flows touch, including `/onboarding/packages/{id}/usage`, `/dme/offers`, `/dme/data-jobs`, `/a1-related/ei-types`, `/sme/published-apis/v1/{apf}/service-apis` |
| **Dashboard** | BFF `GET /api/summary/dashboard` (every tile and fleet count) and `GET /api/summary/attention` ("Needs your attention": critical alarms, pending approvals, MLMF breaches, escalations, the newest three of each with their totals, in one call) · `GET /api/modules/status` (every module's `/health`, `/ready` and `/version`, in parallel; the Module health card has a table of readiness, version, commit and build time, and marks a module running a different commit than most: `moduleRows` in `lib/domain.ts`) · its `panels` carry `/ran-nf-oam/managed-entities/health` and `/worst`, `/alarms/counts?group_by=hour`, `/decision-records?limit=6` (read directly only from a BFF without panels) · on demand `/aimgf/mlmf/reports`, `/rapp-mgmt/instances/{id}/performance`. First load: 3 calls (`src/pages/dashboard/README.md`) |
| **rApps** (call-flow 01) | `/onboarding/packages` (+ `prime`, `deprime`, `deprecate`, `cancel-delete`, `DELETE`, `artifacts`, `usage` + `start`/`stop`) · `/rapp-mgmt/instances` (+ `GET {id}`, `config`, `bootstrap-complete`, `upgrade`, `upgrade/resolve`, `recover`, `terminate`, `DELETE`, `performance`, `faults`) |
| **AI/ML** (call-flow 02) | `/mlmr/models` (+ `{id}` `PUT`/`DELETE`, `artifact`, `artifact/{v}`) · `/mlmr/coordination-groups` · `/aimgf/models/{id}/(advance?event=\|inference-jobs)` · `/aimgf/training-jobs` (+ `model-metrics`) · `/aimgf/inference-jobs` (+ `resolve`) · `/aimgf/mlmf/subscriptions` (+ `reports`) · `/aimgf/feature-groups` · `/mllf/models/{id}/deploy` · `/dme/dme-types` |
| **Alarms** | `/ran-nf-oam/alarms` (filters `managed_element_ref`, `severity`; `PATCH …/ack`, `…/clear`; admin `alarms/ingest`) · `/focom/alarms` |
| **KPIs & Assurance** | `/ran-nf-oam/kpis/{name}` and `/kpis/{name}/series` (the tiles and the KPIs-over-time chart, with `region`, `site_cluster`) · BFF `/api/me/kpi-layouts` (the user's saved chart layouts) · `/rapp-mgmt/instances/{id}/performance` · `/ran-nf-oam/pm-subscriptions` · MLMF as above · `/mdaf/reports`, `subscriptions` · `/ran-analytics/producers` (and, as admin, registering producers and publishing reports via `/mdaf/reports`) · `/sa-smos/monitors` (+ `evaluate`, `remedial-actions`, `escalate`), `/sa-smos/remedial-actions` · `/focom/performance` |
| **Intents** | `/intent-service/intents` (+ `admin-state`), `/intent-reports`, `/intent-handling-functions` · `/intent-service/autonomy-dispatches` (+ `resolve`, `reject`) |
| **Change management** (Infrastructure → O1 endpoints & jobs, the job drawer; KPIs → KPI definitions) | `/ran-nf-oam/config-jobs/{id}` (+ `rollback`, `continue`, `halt`, `abort`), `/ran-nf-oam/kpi-definitions`, `/ran-nf-oam/kpi-schedules`: a staged job's waves, wave actions and rollback with a preview (operator), a job's KPI guard, KPI definitions and schedules (admin) |
| **Safeguards** | `/rapp-mgmt/instances/{id}/safeguards`, `/rapp-mgmt/instances/{id}/kill`, `/ran-nf-oam/rapp-limits/{invokerId}`, `/ran-nf-oam/rapp-kill`, `/ran-nf-oam/safeguard-refusals`, `/ran-nf-oam/safeguard-subscriptions`, `/ran-nf-oam/rapp-approval-policy/{invokerId}`: stop and resume an rApp (operator stops, admin resumes), its limits (admin), whether its config jobs wait for approval, for how long, and whether one person or two different people must approve (admin; the **Approval…** dialog, whose *Approvals needed* defaults to one, and **Stop holding**, shown in the Limits column), the refusal log, and who is told about refusals (admin) |
| **Approvals** (`/approvals`, `PR-GUI-7` steps 7.1-7.3, `AI-11`, `MGT-4`) | `/ran-nf-oam/rapp-approvals` (+ `{id}`, `approve`, `reject`) · `/ran-nf-oam/decision-records?approval_id=` · `/aimgf/model-lifecycles?awaiting_decision=true` (Model gates) · `/ran-nf-oam/config-jobs?status=PENDING_APPROVAL\|SCHEDULED` (+ `approve`, `reject`, `continue`: Change windows): the inbox of rApp actions that wait for a person. *rApp actions* lists what is pending (polled every 5 s: the rApp, its changes, why it asks, when the request lapses and whether it then expires or is rejected); a row opens the request: the changes it would write, the rApp's rationale, model version and inputs reference, and (operator) **Approve** and **Reject** with an optional reason. Who decided is the signed-in user, pinned by the BFF. A request that needs two approvals (the rApp's policy) shows *1 of 2 approvals* on its queue card and, in the detail, who has approved so far; the first approval says the change is not written yet, the person who gave it cannot approve again (the button is disabled; they can still reject), and one rejection ends the request. A request that needs one looks as it always did. *Decided* shows approved, rejected, expired and refused requests (with both approvers when two were needed) and a link to each decision record. Change-window approvals (`7.1`, needs `MGT-4`) and model gate approvals (`7.3`) are not in the inbox yet and the page says so |
| **Decisions** (`/decisions`, `/decisions/{id}`, `AI-13.4`) | `/ran-nf-oam/decision-records` (+ `{id}`) · `/ran-nf-oam/config-jobs/{id}`: why rApps acted. The list filters by rApp, outcome and model version, pages by keyset (`after` / `nextCursor`, *Next* / *Previous*), and `?job=` or `?approval=` narrows it to one job or request. A record shows the outcome, the job and the approval request (each opens in place), who approved (both, when two were needed), the rationale, model version, inputs reference and managed elements, and whether it still matches the audit chain (`VERIFIED`, `UNCHAINED`, `MISMATCH`). The config job drawer (Infrastructure) links to the record of a job an rApp made, with its rationale and approver. **Export…** (operator) starts an export job of the records of the current range, rApp, outcome and scope (BFF `POST /api/exports`) |
| **rApps** (the directory and `/rapps/<instance>`, `PR-GUI-8`) | BFF `GET /api/rapps` (search, `state`, `owner`, `hasPage`, `pinned`), `GET /api/rapps/{instance}`, `/api/rapps/{instance}/operator/...` (only the routes the rApp's package declares), `/api/me/pins`; platform overview of the page: `/rapp-mgmt/instances/{id}` (+ `performance`, `faults`, `safeguards`, `versions`, and the lifecycle buttons); see "The rApp directory and the declared pages" below |
| **Infrastructure** | `/nfo/deployments` (+ `heal`, `scale`, `resources`, `operations`, `DELETE`), `/nfo/descriptors` · `/focom/resource-pools` (+ `resources`, with each resource's CPU and memory from `/focom/utilisation?resource_ids=`), `resources/{id}/utilisation` (the topology inspector), `resource-types`, `deployment-managers`, `topology`, `resources/provision`, `inventory/subscriptions` · `/ran-nf-oam/o1-adaptor-endpoints` (+ `discover`, `heartbeat`), `config-jobs` (several MEs per job), `software-management-jobs` (+ `advance`) · `/so-smos/orders` (+ `cancel`) |
| **Data & Exposure** (call flows 01, 08) | `/dme/dme-types`, `production-capabilities`, `data-jobs`, `offers` (+ `notify`), `type-subscriptions` · `/sme/provider-registrations`, `published-apis/v1/{apf}/service-apis`, `invoker-registrations`, `trusted-invokers`, `service-apis/v1/allServiceAPIs`, `capif-events/v1/{subscriber}/subscriptions` |
| **Admin** | BFF `/api/admin/users`, `/api/admin/audit` (**Export…** starts an audit export job) · RAN access control tab: `/ran-nf-oam/msac/roles`, `/identities`, `/access-rules` (read-only: the BFF has no rule for MSAC writes) |
| **RAN topology** (`/topology`) | `/ran-nf-oam/topology/graph` (the containment tree with each node's open alarms, GUI-3) · `/ran-nf-oam/topology/links` (counts, the focus element's neighbour graph, relations that need attention), `/topology/relation?a&b`, `/topology` (TEIV export), `/managed-entities`, `/cell-guards` |
| **Configuration** (`/configuration`) | `/ran-nf-oam/config-jobs` (+ `halt`, `continue`, `abort`, `rollback`; `kpi-check` is shown read-only: no BFF rule), `/vendor-capabilities`, `/cm-schemas`, `/o1-adaptor-endpoints/{id}/host-keys` (read-only: no BFF rule for re-pinning), `/onboarding-templates` (admin `PUT`, `DELETE`; `MGT-14.6`), `/element-onboarding` (+ `{me}`, `select`, `apply`), `/lifecycle-subscriptions` (admin `POST`, `DELETE`; `MGT-14.7`) |
| **Software** (`/software`) | `/ran-nf-oam/software-campaigns` (+ `{id}`, `report`, `continue`, `halt`, `abort`, `rollback`; a new campaign is dry-run first and may set a job timeout and the rollback order, `MGT-15.5` to `15.7`), `/software-management-jobs`, `/lifecycle-subscriptions` |
| **Element detail** (`/elements/<managed element>`) | `/ran-nf-oam/managed-entities/{me}` (+ `config-history`, `config-history/diff`, `managed-objects/{dn}`, `children`, `subtree`, `cells/{cell}/guards`), `/cell-guards`, `/alarms?managed_element_ref=` |
| **Preferences** (`/preferences`) | BFF `GET`/`PUT /api/me/preferences` |
| **Exports** (`/exports`, operator and admin, `GUI-9.5b`) | BFF `GET /api/exports`, `GET /api/exports/{id}/file`, `DELETE /api/exports/{id}`: the export jobs started from Decisions and the audit log, with state, rows, size, download and delete, refreshed every 2 s while one runs |

Every page also reads the BFF's `GET /api/summary/{page}` for its counts, and the sidebar `GET /api/summary/nav` for its badges. The AI/ML
page adds a **Registry** tab (`/mlmr/ml-model-repositories`, `/storages`) and a model's governance and lifecycle history
(`/aimgf/models/{id}/governance-history`, `/lifecycle-history`); Intents adds **Utility formulas** (`/intent-service/intent-utility-formulas`);
KPIs adds MDA functions, requests and report files (`/mdaf/mda-functions`, `/mda-requests`, `/mda-reports/{id}/file`). Negotiation
feedback on an intent and requesting an MDA analysis are drawn but have no button yet: the BFF's permission table does not allow those two
routes (`OPEN_ITEMS.md` `PR-GUI-9`).

**Scope** (`GUI-9.3`): the top bar's picker narrows the console to a region and, inside it, a site cluster (RAN NF OAM's
`GET /managed-entities/scopes` lists them with their element counts). The choice is in the URL (`?region=&cluster=`), kept by the sidebar's
links and given back by the router to any navigation that drops it (`src/shell/ScopeProvider.tsx`); "All network" clears it. Every read of a
route that takes `region` / `site_cluster` gets them (`src/data/scope.ts` `SCOPED_ROUTES`, applied once in `api/hooks.ts`), and so do the
summaries and the event topics (`summary:<page>@<region>[/<cluster>]`). A box whose data the scope cannot narrow (the BFF's `unscoped` count
keys, and the safeguard refusals, which record no element) says "network-wide" (`src/kit/ScopeNote.tsx`).

Live updates: each tab opens one Server-Sent Events stream (`GET /api/events?topics=summary:nav,summary:<page>`, plus `summary:attention` on the Dashboard, each scoped when a scope is picked; `src/data/events.ts`,
`src/shell/LiveEvents.tsx`) while it is visible; each pushed summary is written into the summary cache and refetches only the lists whose counts
changed (an alarm count change refetches the alarm list, a pending-approval change the approval queue), and the top bar says "Live · pushed".
While it is open the summary counts and the alarm list are re-read only once a minute. Without it (connecting, refused, no EventSource) the
console polls, top bar "Live · polling": alarms every 5 s, module health every 10 s, lists every 15 s, summary counts every 15 s (the sidebar's
every 30 s), inventory every 60 s, and only while the tab is visible (TanStack Query). The stream reconnects with a 1 s → 60 s back-off. An action refetches the reads of its own module, of the modules a call there is known
to change too (`src/data/keys.ts`, `CROSS_MODULE`: a rApp instantiation also changes NFO and Onboarding), and the counts.

## The rApp directory and the declared pages

The sidebar has one **rApps** entry and, under it, the rApps the signed-in user **pinned** (at most five, kept by the BFF in `gui_rapp_pin`, so they follow the user to any browser). **rApps** opens the *Directory* tab: every rApp instance with its name, version, owner (the package's vendor), state, autonomy mode and whether it declares a page; search (after a 250 ms pause) by name, owner, version or id; filters for state, owner, "declares a page" and "pinned only"; paging by 25; a star pins or unpins. *Packages* and *Instances* are the other two tabs (onboarding, deploying, upgrading), unchanged; the Deploy dialog has an optional *Operator API base URL*.

A row opens `/rapps/<instance>`. It shows, first, **the page the rApp's package declares** and, under *Platform overview*, what every rApp has: lifecycle (with its buttons), the KPIs it reported, safeguards, faults and version history. The declaration is `operatorUi` in the package's `manifest.yaml` (`../docs/adr/0004-operator-ui-declaration.md` has the format and its limits; `../docs/RAPP_PACKAGING.md` an example; `smo_sdk.operator_ui` writes one). The renderer is `src/components/OperatorUi.tsx` over `src/lib/operatorUi.ts` (pure, tested in `operatorUi.test.ts`), so **a rApp onboarded at run time shows its page with no GUI build** and nothing a rApp ships runs in the browser (no JavaScript, no iframe).

| Declared | Drawn as |
|---|---|
| `table` | A table: `rows` (the list in the answer), `rowKey`, columns with a format (`text`, `number`, `percent` (the number is already in percent), `datetime`, `badge`, `id`, `boolean`, `list`, `sparkline`), `empty` text; a click on a row opens its `rowDetail` drawer; `rowActions` are buttons per row, shown only when their `when` holds for that row |
| `keyValues` | Labelled values of one object |
| `kpis` | Number tiles: `path` reads the panel's source, `kpi` the latest performance report of the instance (a page of only `kpi` tiles needs no operator API) |
| `chart` | A line or bar chart (SVG) of `points`, `x` and `y`, one series per `seriesBy` value (at most eight; the rest are counted, not drawn), with a legend |
| `actions` | Buttons. `inputs` open a dialog (checked before sending), `confirm` asks first, the declared `success` text is the toast, `tone` styles the button |
| `rowDetail` | A drawer with a title (`{row.<field>}` filled from the clicked row) and up to six blocks: `json` (the row or a field of it, with an `empty` text), `keyValues`, `table` and `chart` (from a list field of the row, or fetched for the open row when the drawer opens) |

- **Reads** go to `/api/rapps/<instance>/operator/<route>` and are repeated every `refreshSeconds` of the source (5 to 3600; none: when the page opens and by the panel's refresh button). **Changes** are sent with the declared action's id, and the BFF fills `"{user}"` and refuses anything the declaration does not list (`../gui-bff/README.md` 2.4); a **viewer sees no change button**, only a sentence saying why. A page whose rApp registered no operator API says so and does not request its panels.
- **Unknown things never break the page.** A panel kind this build does not know is a card saying *unsupported panel*; an unknown column shape or a chart of an unknown type likewise; an unknown block kind is *unsupported block*; an unknown format is text; a panel that throws is *could not be drawn* and the others are drawn. A missing field is a dash.
- **Everything on the page is text.** A title of `<script>` or a value of `<img onerror>` is drawn as those characters; a badge's colour comes from the GUI's table of state words, never from the value; field paths follow only names the object itself has.
- What a declared page cannot show (composed text such as `source → target`, a map's entries, threshold colours, a second drawer level) is listed at the end of the ADR; a rApp that needs one adds a field to its answer.

## Out of scope (Phase 1)

- Alarm-storm correlation (alarms carry `correlationGroup` /
  `correlatedNotifications`, but the GUI doesn't compute storms).
- Live KPI file collection: PM subscriptions register DME producer types,
  and the counters themselves aren't collected (as in RAN NF OAM).
- Southbound Docker/NETCONF beyond what the modules already stub; results
  of inference jobs (pulled through DME, not shown here).
- R1 Termination's own OAuth is unchanged. The GUI's users and roles live in
  the BFF, not an external IdP.

## Screenshots

Every screenshot here is taken by `../scripts/gui_screenshots.py`, a Playwright walk-through of the redesigned console (its docstring says how to
run it and what it needs). It ran against a stack started fresh with `docker compose up` (every module, R1 Termination, the BFF and the GUI, on
Postgres) and seeded by the four sample-rApp demos (`samples/*/demo.py all`, run inside `r1-termination`). The script signs in through the
sign-in form and then makes the state itself, through the GUI's own forms, dialogs and buttons: it onboards and deploys packages, reports faults
with the admin tools, trains a model to an active runtime, raises and clears alarms, runs a service order and CM writes, and so on. Where a step
needs another party it acts as that party the way `../DEMO_RUNBOOK.md` does: a new package version is built from `samples/energy-saving-rapp` and
copied into `r1-termination`'s CSAR server, and a failed training run is reported through the BFF. Each run uses its own names (a run id in
versions, model types and labels), so a screen can show rows of earlier runs too. Signed in as `admin`, 1440 px wide, dark theme, unless the
caption says otherwise; a drawer or dialog is shown whole (the window is made as tall as its content). A full walk-through takes about 17 minutes:

    GUI_E2E_PASSWORD=... GUI_E2E_OPERATOR_PASSWORD=... GUI_E2E_VIEWER_PASSWORD=... python scripts/gui_screenshots.py [--only lcm-rapps]

Lifecycle (LCM) screens are in the order the walk-through takes them, so a module's rows read as a story: the state before an action, then the
state after it.

### Generic

| Screen | What it shows |
|---|---|
| [Sign in](docs/screenshots/generic/login.png) | The only page reachable signed out. With OIDC on it also offers "Sign in with <provider>"; an account with a one-time code gets a second step (not shown: the default is password only) |
| [Failed sign-in](docs/screenshots/generic/login-failed.png) | Wrong password: a generic error that does not say which half was wrong |
| [Account locked](docs/screenshots/generic/login-locked.png) | After 5 failed attempts the name is locked for 5 minutes; the message does not say whether the user exists |
| [Dashboard](docs/screenshots/pages/dashboard.png) | Network health, open alarms by severity, autonomous actions, approvals and model guard breaches; the region health map, worst elements and what needs attention |
| [Dashboard, light theme](docs/screenshots/pages/dashboard-light.png) | The same page in the light theme |
| [Dashboard, scoped](docs/screenshots/pages/dashboard-scoped.png) | Scope eu-west (top bar): health score, alarms, attention and fleet counts for that region; the boxes the scope cannot narrow say "network-wide" |
| [Alarms, scoped](docs/screenshots/pages/alarms-scoped.png) | Region eu-west, site cluster metro-a: the severity tiles and the table count only that cluster's elements |
| [Exports](docs/screenshots/pages/exports.png) | Background CSV exports (Decisions, audit log): state, rows, size, kept until, Download and Delete |
| [Search (⌘K)](docs/screenshots/generic/search.png) | The top bar's search: page jumps plus the elements, rApps, alarms, models and decisions matching "gnb" |
| [Change password](docs/screenshots/generic/change-password-dialog.png) | Any signed-in user, from the sidebar's user card |
| [Preferences](docs/screenshots/pages/preferences.png) | Theme, accent, text size and display defaults (start page, rows per page, time zone…), kept by the BFF per user |
| [Preferences, light theme](docs/screenshots/pages/preferences-light.png) | The same page in the light theme |
| [Account security](docs/screenshots/pages/security.png) | Two-step sign-in, recovery codes and the user's own recent sign-ins |
| [Users and roles](docs/screenshots/pages/admin-users.png) | Admin: users with role and last activity, deactivate / reset password / revoke sessions, and the role matrix the BFF enforces |
| [Add user](docs/screenshots/generic/admin-add-user-dialog.png) | Admin: create a GUI user with a role |
| [Audit log](docs/screenshots/generic/admin-audit-log.png) | Admin: the append-only log of every mutating call (allowed or denied), sign-ins and user administration, with an export job |
| [MSAC](docs/screenshots/pages/admin-msac.png) | Admin: the MSAC tiers that gate wide-scope configuration writes |

### Lifecycle flows (`/flows`)

Each board follows one subject (the picker at the top), proves each step from live state and offers the next action.

| Flow | What the board shows |
|---|---|
| [Flow board (list)](docs/screenshots/pages/flows.png) | The flow journeys, each with its progress; pick one, then the subject |
| [01 rApp onboarding → running instance](docs/screenshots/flows/f01.png) | A demo package: onboarded, descriptor created, instance deployed on NFO; 5/6, waiting for the bootstrap (Mark bootstrapped) |
| [02 AI/ML model: register → train → certify → deploy → infer → monitor](docs/screenshots/flows/f02.png) | The walk-through's model: certified, node groups cleared, runtime ACTIVE, inference jobs and an MLMF floor breach |
| [03 Configuration write, schema-checked, fleet-aware](docs/screenshots/flows/f03.png) | The two-element CM write: `PARTIAL_SUCCESS`, one element APPLIED, one REJECTED (`NETCONF_UNREACHABLE`); the endpoints offer Heartbeat |
| [04 Closed-loop assurance: monitor → decide → remediate → escalate](docs/screenshots/flows/f04.png) | An order-scoped monitor: thresholds, MDAF and MLMF reports, `RECONNECT` RESOLVED, an escalation; 5/5 |
| [06 Package lifecycle (failed validation)](docs/screenshots/flows/f06.png) | A package that failed validation (a second onboarding of the demo's CSAR): CreateInstance is refused, Delete is the next action |
| [06 Package lifecycle (guard blocking)](docs/screenshots/flows/f06-blocked.png) | A PRIMED package with active usage registrations: the cascade-delete guard is the failing step, with Stop usage offered |
| [07 rApp instance lifecycle](docs/screenshots/flows/f07.png) | An instance that reported performance, a minor and a critical fault, was recovered and bootstrapped again; 6/7, Terminate next |
| [08 RAN Analytics: producer → report → subscriber query](docs/screenshots/flows/f08.png) | Producer registered and discoverable at SME, reports published; the subscribe step is offered |
| [09 Intent registration → fulfilment reporting → admin state](docs/screenshots/flows/f09.png) | The walk-through's intent: addressed to SA SMOS, handler reports received, deactivated by its RMIO (`smo-gui`); 5/5 |
| [10 SO SMOS multi-step order: INFRA → TRAINING → DEPLOY](docs/screenshots/flows/f10.png) | The walk-through's order: three steps dispatched to FOCOM, AI/ML Workflow and NFO, all COMPLETED |
| [15 NFO workload: instantiate → scale → heal → terminate](docs/screenshots/flows/f15.png) | A service order's NF deployment, RUNNING; Scale and Terminate offered |
| [16 FOCOM resource & inventory](docs/screenshots/flows/f16.png) | A provisioned O-Cloud resource with the inventory subscriptions it matches; Deprovision next |
| [19 RAN software job: download → install → activate](docs/screenshots/flows/f19.png) | The walk-through's software job: DOWNLOAD done, now in INSTALL with ok / failed offered |

### By module

Each table: the page's tabs first, then the lifecycle screens for that module.

#### rApps: packages and instances (Onboarding, rApp Management)

| Screen | What it shows |
|---|---|
| [Directory (tab)](docs/screenshots/pages/rapp-directory.png) | Every rApp instance with state, autonomy mode, whether it declares a page, and the pin star; search and filters |
| [Instances (tab)](docs/screenshots/pages/rapps-instances.png) | rApp instances with state, the flow 07 lifecycle cell, autonomy mode, headline KPI and actions |
| [Packages (tab)](docs/screenshots/pages/rapps-packages.png) | The onboard form, the package pipeline counts (each a filter) and the packages with state, signature, NF descriptor and actions |
| [Rollouts (tab)](docs/screenshots/pages/rapps-rollouts.png) | Instances being upgraded right now, with Upgrade succeeded / failed |
| [Package: failed validation](docs/screenshots/lcm/rapps-package-failed.png) | The FAILED filter after onboarding the demo's CSAR a second time (same content hash): nothing to deploy, only Delete |
| [Onboard a package](docs/screenshots/lcm/rapps-onboard-form.png) | The CSAR location of a new version (1.1), filled but not submitted |
| [Package: onboarded](docs/screenshots/lcm/rapps-package-onboarded.png) | Version 1.1 AVAILABLE after Onboarding validated it, with Deploy, Prime, Deprecate and Delete |
| [Package: AVAILABLE](docs/screenshots/lcm/rapps-package-drawer.png) | The package drawer: identity, AI capabilities, ASD descriptor, SME declarations, artifacts, Prime, usage registrations |
| [Deploy dialog](docs/screenshots/lcm/rapps-deploy-dialog.png) | Instance configuration and autonomy mode (ASSIST), fixed for the instance's lifetime; optional operator API base |
| [Package: PRIMED, deprime blocked](docs/screenshots/lcm/rapps-package-primed-deprime-blocked.png) | Deprime is disabled while two usage registrations are active (the instance and a test usage), with the reason |
| [Instance: RUNNING](docs/screenshots/lcm/rapps-instance-drawer.png) | The bootstrapped instance: provenance, configuration, version history, performance sparklines and a minor fault, reported with the admin tools |
| [Instance: FAULTED](docs/screenshots/lcm/rapps-instance-faulted.png) | After a critical fault report: Recover and Terminate |
| [Instance: recovering](docs/screenshots/lcm/rapps-instance-recovering.png) | Recover re-enters DEPLOYING until the rApp bootstraps again (Mark bootstrapped) |
| [Instance: RUNNING again](docs/screenshots/lcm/rapps-instance-recovered.png) | Bootstrapped again; the fault history remains |
| [Upgrade dialog](docs/screenshots/lcm/rapps-upgrade-dialog.png) | Pick an AVAILABLE package (version 1.2); the upgrade runs as two rows, resolved success / failure |
| [Upgrade: UPGRADING](docs/screenshots/lcm/rapps-upgrade-in-progress.png) | The instance names its pending replacement; Upgrade succeeded / Upgrade failed |
| [Upgrade: replacement DEPLOYING](docs/screenshots/lcm/rapps-upgrade-replacement-deploying.png) | The replacement instance on version 1.2, awaiting its bootstrap |
| [Upgrade: committed](docs/screenshots/lcm/rapps-upgrade-committed.png) | The replacement is RUNNING, its last teardown is the UPGRADE_COMMIT of the old instance, the history shows the UPGRADE and Roll back is offered |
| [Rollback: UPGRADING](docs/screenshots/lcm/rapps-rollback-in-progress.png) | Roll back starts a replacement on the previous package and configuration |
| [Rollback: committed](docs/screenshots/lcm/rapps-rollback-committed.png) | The history now shows ROLLBACK, and the upgrade it undid is marked rolled back |
| [Instance: UNDEPLOYED](docs/screenshots/lcm/rapps-instance-terminated.png) | After Terminate: NFO teardown and usage stop recorded under Last teardown; Delete remains |
| [Package: PRIMED, usage released](docs/screenshots/lcm/rapps-package-primed-usage-released.png) | Every usage registration stopped, so Deprime is enabled |
| [Package: AVAILABLE again](docs/screenshots/lcm/rapps-package-deprimed.png) | Deprime succeeded (PRIMED → DEPRIMING → AVAILABLE) |
| [Package: DEPRECATED](docs/screenshots/lcm/rapps-package-deprecated.png) | After Deprecate: no new instances; Restore and Delete remain |
| [Package: DELETING](docs/screenshots/lcm/rapps-package-delete-requested.png) | Delete requested; the cascade-delete guard found no active usage |

#### rApp directory and declared pages (`/rapps`, `/rapps/<instance>`)

| Screen | What it shows |
|---|---|
| [Energy Saving page](docs/screenshots/pages/rapp-page-energy-saving.png) | The page declared in the package: instance, closed-loop buttons, cells with sparklines and row actions, then the platform overview |
| [Mobility page](docs/screenshots/pages/rapp-page-mobility.png) | The same renderer on another package, as an admin |
| [Mobility page, as a viewer](docs/screenshots/pages/rapp-page-mobility-viewer.png) | No change button, and a sentence saying why |
| [Mobility: relation drawer](docs/screenshots/pages/rapp-drawer-mobility.png) | The drawer of a row: trend chart, the latest execution as JSON, the history fetched for that row |
| [Pinned rApp in the sidebar](docs/screenshots/pages/rapp-sidebar-pinned.png) | A pinned rApp under the one rApps entry |

**Tenant and region scope (`SEC-10`).** The O1 endpoints table shows where each managed element is and whom it belongs to (`region / tenant`, a dash for what is not set), and the rApp page (Platform overview, Lifecycle) shows the instance's *Access scope*: the regions and tenants it may touch, or "Unscoped (every managed element)". The Safeguards page lists a refusal for a scope (`SCOPE_DENIED`) with the other refusals. The claim and an element's place are set through the API for now (`OPEN_ITEMS.md`, `SEC-10.10`); the console itself is not scoped (`GUI-5.1`).

#### Approvals, decisions and safeguards

| Screen | What it shows |
|---|---|
| [Approvals](docs/screenshots/pages/approvals.png) | rApp changes waiting for a person, with the lapse countdown, and the decided ones |
| [Approvals → two people](docs/screenshots/pages/approvals-two-person.png) | A request whose rApp's policy asks two different people: "1 of 2 approvals" on its card, who approved so far, and the decision box saying the next approval is the last one needed (`PR-AI-11` follow-up) |
| [Decisions](docs/screenshots/pages/decisions.png) | Why each rApp change was made: filters, tiles and the decision records, with the chain inputs → model → config job → approval |
| [Safeguards](docs/screenshots/pages/safeguards.png) | Stop all rApp writes, per-rApp limits, refusals and watchers |

#### AI/ML (MLMR, AIMgF, MLLF, MLMF)

| Screen | What it shows |
|---|---|
| [Models (tab)](docs/screenshots/pages/aiml-models.png) | Models by stage (board), training now and waiting for governance |
| [Training jobs (tab)](docs/screenshots/pages/aiml-training.png) | Jobs with status, step, progress, runtime and metrics; Suspend / Resume / Cancel |
| [Inference jobs (tab)](docs/screenshots/pages/aiml-inference.png) | Inference jobs per model with status and runtime |
| [Feature groups (tab)](docs/screenshots/pages/aiml-features.png) | The new feature group form and the registered datalake feature groups |
| [Coordination groups (tab)](docs/screenshots/pages/aiml-groups.png) | The new group form and groups of models retrained together |
| [Performance monitoring, MLMF (tab)](docs/screenshots/pages/aiml-mlmf.png) | The subscribe form and model-performance subscriptions with a KPI floor |
| [Registry (tab)](docs/screenshots/pages/aiml-registry.png) | MLMR repositories, storages and artifact versions |
| [Register model](docs/screenshots/lcm/aiml-register-model-dialog.png) | Type, version, description, owner, resource type and data types; filled but not submitted |
| [Model: TRAINING](docs/screenshots/lcm/aiml-model-training.png) | Pipeline stepper, the training job in progress, Training complete |
| [Model: TRAINED, approval gate](docs/screenshots/lcm/aiml-model-trained-approval-gate.png) | After the training job finishes, an operator must approve before validation |
| [Model: VALIDATING](docs/screenshots/lcm/aiml-model-validating.png) | After the training approval and Request validation |
| [Model: VALIDATED, approval gate](docs/screenshots/lcm/aiml-model-validated-approval-gate.png) | A second operator gate before emulation |
| [Model: EMULATED](docs/screenshots/lcm/aiml-model-emulated-submit.png) | Submit for approval opens the governance steps; the governance history lists both approvals |
| [Model: PENDING_APPROVAL](docs/screenshots/lcm/aiml-model-pending-approval.png) | Approve or Reject |
| [Model: CERTIFIED](docs/screenshots/lcm/aiml-model-certified.png) | Promote, Retrain or Deprecate; the deploy-to-node-groups form appears |
| [Model: PROMOTED](docs/screenshots/lcm/aiml-model-promoted-deploy-node-groups.png) | The Deploy to node groups field, filled |
| [Deploy targets cleared](docs/screenshots/lcm/aiml-model-node-groups-cleared.png) | `clearedNodeGroups` stamped by MLLF; Deploy runtime offered |
| [Runtime: DEPLOYED](docs/screenshots/lcm/aiml-model-runtime-deployed.png) | AIMgF and NFO deployed the runtime; Activate or Terminate |
| [Runtime: ACTIVE, inference requested](docs/screenshots/lcm/aiml-model-runtime-active-inference-requested.png) | An inference job RUNNING, with Completed / Failed (AIMgF fails a job left running for 5 s) |
| [Model: PROMOTED, runtime ACTIVE](docs/screenshots/lcm/aiml-model-promoted-runtime-active.png) | Cleared node groups, governance history, runtime actions, the training job and two inference jobs (COMPLETED, FAILED) |
| [Write back model metrics](docs/screenshots/lcm/aiml-training-metrics-dialog.png) | What the trainer reports for a job; replaces the stored metrics |
| [Inference jobs](docs/screenshots/lcm/aiml-inference-job-running.png) | COMPLETED and FAILED outcomes across models |
| [Model: retrain requested](docs/screenshots/lcm/aiml-model-retrain-requested.png) | A second model whose training run failed: Retry training started a new job (the FAILED one stays in the list) |
| [New coordination group](docs/screenshots/lcm/aiml-coordination-group-form.png) | Two members picked, use cases entered |
| [MLMF: subscription and reports](docs/screenshots/lcm/aiml-mlmf-subscription-and-reports.png) | The subscription, then three reports: two above the floor, one BREACHED, drawn against the floor |
| [New feature group](docs/screenshots/lcm/aiml-feature-group-form.png) | Name, features and datalake settings; the token field is masked |

#### Alarms (RAN NF OAM O1 FM, FOCOM)

| Screen | What it shows |
|---|---|
| [RAN NF alarms (tab)](docs/screenshots/pages/alarms-ran.png) | Severity tiles (each a filter), time to acknowledge, raised per hour, the alarm table with its server filters, the detail panel and the injection tool |
| [RAN NF alarms, light theme](docs/screenshots/pages/alarms-ran-light.png) | The same tab in the light theme |
| [O-Cloud alarms (tab)](docs/screenshots/pages/alarms-ocloud.png) | Infrastructure alarms from FOCOM |
| [Filter: critical](docs/screenshots/lcm/alarms-filter-critical.png) | Two alarms injected with the admin tool (what the element's NotifyNewAlarm carries); the critical tile filters the list |
| [Alarm: raised](docs/screenshots/lcm/alarm-raised-unacknowledged.png) | 3GPP TS 28.532 / 28.111 fault fields, the lifecycle timeline and the likely root cause; UNACKNOWLEDGED |
| [Alarm: acknowledged](docs/screenshots/lcm/alarm-acknowledged.png) | Ack recorded against the GUI user, with the time |
| [Alarm: cleared](docs/screenshots/lcm/alarm-cleared.png) | Clear recorded with who and when; Unack remains |
| [List with cleared alarms](docs/screenshots/lcm/alarms-with-cleared.png) | *show cleared* includes the cleared alarms |

#### KPIs & Assurance (RAN NF OAM PM, MDAF, SA SMOS, MLMF, FOCOM)

| Screen | What it shows |
|---|---|
| [Overview (tab)](docs/screenshots/pages/kpis-overview.png) | KPI tiles over the chosen range, assurance monitors, escalations and the worst elements (taken before the KPIs-over-time box, GUI-4) |
| [PM subscriptions (tab)](docs/screenshots/pages/kpis-pm.png) | The new subscription form and counter subscriptions per managed element |
| [rApp performance (tab)](docs/screenshots/pages/kpis-rapp.png) | Sparklines per reported metric |
| [RAN Analytics (tab)](docs/screenshots/pages/kpis-analytics.png) | MDA requests and reports, the analytics reports, producers and subscriptions, and the producer tools |
| [Assurance, SA SMOS (tab)](docs/screenshots/pages/kpis-assurance.png) | The register form, monitors and remedial actions with outcomes |
| [O-Cloud performance (tab)](docs/screenshots/pages/kpis-ocloud.png) | FOCOM performance |
| [Model KPIs, MLMF (tab)](docs/screenshots/pages/kpis-mlmf.png) | The MLMF tab of the AI/ML page, here too |
| [New PM subscription](docs/screenshots/lcm/kpis-pm-subscription-form.png) | Managed element, counter, delivery and granularity |
| [Analytics report](docs/screenshots/lcm/kpis-analytics-report-dialog.png) | One report's output, published with the producer tools as `energy-saving-rapp` |
| [Register assurance monitor](docs/screenshots/lcm/kpis-register-monitor-form.png) | Scope picked from the SO SMOS orders, metric floors entered |
| [Monitor registered](docs/screenshots/lcm/kpis-monitor-registered.png) | The new monitor listed |
| [Monitor: threshold breach](docs/screenshots/lcm/kpis-monitor-evaluate-breach.png) | Evaluate with a metric below its floor: the breach comes back inline |
| [Remedial action: RESOLVED](docs/screenshots/lcm/kpis-remedial-action-resolved.png) | `RECONNECT` executed against the order's NF deployment; the monitor counts one action taken |
| [Escalated to operator](docs/screenshots/lcm/kpis-escalated-to-operator.png) | Escalate with a reason; the outcome is ESCALATED |
| [Group-scoped remediation](docs/screenshots/lcm/kpis-group-retrain-remediation.png) | A monitor on a model coordination group: any action type retrains the group (scope "model group (retrain)", RESOLVED) |

#### Intents (Intent Service)

| Screen | What it shows |
|---|---|
| [Intents (tab)](docs/screenshots/pages/policy-intents.png) | Tiles, TS 28.312 intents with admin state and fulfilment, and the new-intent form |
| [Intent handlers, RMIH (tab)](docs/screenshots/pages/policy-handlers.png) | Framework-internal handlers and their capabilities |
| [Autonomy dispatches (tab)](docs/screenshots/pages/policy-autonomy.png) | The request form and dispatches by autonomy mode |
| [Utility formulas (tab)](docs/screenshots/pages/policy-formulas.png) | The registered intent utility formulas |
| [Create intent](docs/screenshots/lcm/policy-intent-create-form.png) | Handler SA SMOS, label, object type, a target the handler declares (energy-saving control), priority and purpose; checked against the handler |
| [Intent: ACTIVATED](docs/screenshots/lcm/policy-intent-drawer.png) | Expectations and the handler's reports: RECEIVED, then DEGRADED (the expectation names no managed element) |
| [Intent: DEACTIVATED](docs/screenshots/lcm/policy-intent-deactivated.png) | Admin state changed by the intent's own RMIO (`smo-gui`) |
| [Publish an intent report](docs/screenshots/lcm/policy-intent-report-form.png) | The admin tool for acting as the handling RMIH |
| [Intent report published](docs/screenshots/lcm/policy-intent-report-published.png) | The new fulfilment report appears first in the intent's reports |
| [Autonomy: AWAITING_SCOPE](docs/screenshots/lcm/policy-autonomy-awaiting-scope.png) | An ASSIST instance asked for a dispatch: it waits for an operator to scope it, or reject it |
| [Autonomy: resolved](docs/screenshots/lcm/policy-autonomy-resolved.png) | Scope supplied; an intent was created and the dispatch is DISPATCHED |

#### RAN pages (RAN NF OAM)

| Screen | What it shows |
|---|---|
| [RAN topology](docs/screenshots/pages/topology.png) | Relation tiles, the cell graph and the problem table (relations not reciprocal, external, ambiguous) |
| [Configuration](docs/screenshots/pages/configuration.png) | CM write jobs with staged waves, halts, rollback and KPI guards; the Element onboarding tab (`MGT-14.6`, `MGT-14.7`) has the onboarding templates (admin), each element's onboarding with **Apply** / **Select…** where the state allows (operator), and who is told when an onboarding fails (no screenshot of that tab yet) |
| [Configuration → Element onboarding](docs/screenshots/pages/configuration-onboarding.png) | Onboarding templates (`MGT-14.6`: applies to type · vendor, changes, baseline, auto-apply; admin edits) and each element's onboarding row with Apply / Select as its state allows (`MGT-14.7` failure notices below) |
| [Software](docs/screenshots/pages/software.png) | Software campaigns in waves with their gates; a campaign's job timeout and rollback order (`MGT-15.6`, `MGT-15.7`), jobs marked "timed out", and who is told when a campaign halts or a rollback fails (not in the screenshot yet) |
| [Software → New campaign](docs/screenshots/pages/software-new.png) | The new-campaign form: selector or element list, waves, pause, health gate, job timeout (`MGT-15.6`) and rollback order (`MGT-15.7`, last wave first by default); dry run before Start |
| [Element](docs/screenshots/pages/element.png) | One managed element (`gnb-du-demo-01`): overview, alarms, cells and guards |
| [Element: managed objects](docs/screenshots/pages/element-mos.png) | The same element's managed-object tree |

#### Infrastructure (NFO, FOCOM, RAN NF OAM, SO SMOS)

| Screen | What it shows |
|---|---|
| [Topology (tab)](docs/screenshots/pages/infra-topology.png) | TEIV entities and relationships exported from FOCOM |
| [NF deployments (tab)](docs/screenshots/pages/infra-nfo.png) | State tiles (each a filter), deployments with heal / scale / terminate, and the descriptors |
| [O-Cloud inventory (tab)](docs/screenshots/pages/infra-ocloud.png) | The level picker (locations → sites → clusters…, provisioning, O2-IMS pools) and inventory subscriptions |
| [O1 endpoints & jobs (tab)](docs/screenshots/pages/infra-o1.png) | Managed elements (with region and tenant, `SEC-10.2`), CM write jobs and software management jobs |
| [Service orders (tab)](docs/screenshots/pages/infra-orders.png) | Orders as steppers coloured by step status, with Details |
| [Compose service order](docs/screenshots/lcm/orders-compose.png) | INFRA, TRAINING and DEPLOY step templates added to the order (prefilled with the newest model and an unused NF descriptor) |
| [Service order: executed](docs/screenshots/lcm/order-drawer-steps.png) | Per-step results: resource, training job, NF deployment, all COMPLETED |
| [NF deployment: RUNNING](docs/screenshots/lcm/nfo-deployment-running.png) | The order's deployment: LCM operations and linked O-Cloud resources |
| [NF deployment: heal and scale](docs/screenshots/lcm/nfo-heal-and-scale-operations.png) | HEAL and SCALE operations recorded |
| [NF deployment: ABNORMAL](docs/screenshots/lcm/nfo-runtime-failure-abnormal.png) | A deployment-manager `RUNTIME_FAILURE` (reported from the drawer) with its reason |
| [NF deployment: healed](docs/screenshots/lcm/nfo-healed-running.png) | Heal returns it to RUNNING |
| [O-Cloud resource provisioned](docs/screenshots/lcm/ocloud-resource-provisioned.png) | A new resource in pool-0, with Deprovision |
| [Inventory-change subscription](docs/screenshots/lcm/ocloud-inventory-subscription-form.png) | Callback and resource type filter, filled |
| [Subscription created](docs/screenshots/lcm/ocloud-inventory-subscription-created.png) | Notified on provision / deprovision of matching resources |
| [Register O1 endpoint](docs/screenshots/lcm/o1-register-endpoint-dialog.png) | Managed element, entity type, adaptor URI, protocol, vendor, region and tenant |
| [O1 health discovery](docs/screenshots/lcm/o1-health-discovery.png) | Run health discovery re-ages every endpoint's health |
| [New CM write](docs/screenshots/lcm/o1-cm-write-dialog.png) | Two managed elements (one whose adaptor does not answer), scope, operation, schema-checked changes, staged rollout and KPI guard |
| [CM write submitted](docs/screenshots/lcm/o1-cm-write-submitted.png) | The new job in the list |
| [CM job: PARTIAL_SUCCESS](docs/screenshots/lcm/o1-cm-job-partial-success.png) | Per-element sub-changes: APPLIED and REJECTED (`NETCONF_UNREACHABLE`); Roll back offered |
| [CM write to a RESTCONF ME](docs/screenshots/lcm/o1-cm-write-restconf-applied.png) | An element registered with RESTCONF at its RFC 8040 root accepts the write: one sub-change APPLIED |
| [Software management job](docs/screenshots/lcm/o1-software-job-advanced.png) | A job advanced past DOWNLOAD to its next phase (INSTALL) |

#### Data & Exposure (DME, SME)

| Screen | What it shows |
|---|---|
| [DME: flow & jobs (tab)](docs/screenshots/pages/data-dme.png) | Producers → data types → consumers, and the data jobs with the create form |
| [DME: producers & offers (tab)](docs/screenshots/pages/data-offers.png) | Producers, data types, offers and type subscriptions |
| [SME: services & invokers (tab)](docs/screenshots/pages/data-sme.png) | Providers and published APIs, invokers, discovery and CAPIF event subscriptions |
| [Register a producer data type](docs/screenshots/lcm/dme-register-type-form.png) | The admin tool for acting as a producer |
| [DME offer: notify data ready](docs/screenshots/lcm/dme-offer-notify-data-ready.png) | An offer for the new type (DME committed a method), and the producer's availability signal |
| [Create a data job](docs/screenshots/lcm/dme-data-job-form.png) | Type, mode, delivery (the hint names the committed method) and consumer |
| [Data job](docs/screenshots/lcm/dme-data-job-dialog.png) | The created job's record |
| [SME invoker secret](docs/screenshots/lcm/sme-invoker-secret-dialog.png) | Shown once and never again; SME keeps only a hash |
| [Trusted-invoker security context](docs/screenshots/lcm/sme-trusted-invoker-dialog.png) | Notification destination, AEF, API and preferred security method |
| [CAPIF event subscription form](docs/screenshots/lcm/sme-event-subscription-form.png) | Subscriber, events, callback and optional filters |
| [CAPIF event subscription created](docs/screenshots/lcm/sme-event-subscription-created.png) | Listed under its subscriber |

### Role views

Each screen is the same live state seen through a lower role. Actions the role can't perform aren't rendered, and the BFF refuses them anyway.

| Role | Screens |
|---|---|
| Viewer | [dashboard](docs/screenshots/roles/viewer-dashboard.png) · [alarms](docs/screenshots/roles/viewer-alarm-list.png) · [flow 06](docs/screenshots/roles/viewer-flows-06.png) · [rApp packages](docs/screenshots/roles/viewer-rapp-packages.png) · [SME](docs/screenshots/roles/viewer-sme.png) |
| Operator | [dashboard](docs/screenshots/roles/operator-dashboard.png) · [rApp instances](docs/screenshots/roles/operator-rapp-instances.png) · [DME](docs/screenshots/roles/operator-dme.png) · [infrastructure](docs/screenshots/roles/operator-infrastructure.png) |
