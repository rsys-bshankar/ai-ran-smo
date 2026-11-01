# SMO Operator GUI redesign — implementation brief for Claude Code

Repo: `rsys-bshankar/ai-ran-smo`, package `smo/gui` (React 19, Vite, react-router 7, TanStack Query). Designs were made against commit `12a1b90`.

**Goal:** a bold visual refresh and richer pages across the whole console, for customer use. Keep every existing feature, route, RBAC check (`ActionButton`, `Can`) and test passing. This is a front-end change. Where a design needs data the BFF doesn't serve today, build the UI behind the existing data and list the gap (see the end of this brief). Don't invent backend values.

**What's in this hand-off folder:**

- `BRIEF.md`: this file, covering what to build.
- `SCALE.md`: the scale and latency checklist for every page and box (targets, today's bugs, patterns P1–P12, call budgets). **Read it before section 4.**
- `STRUCTURE.md`: one folder per page with one file per section, the page README template, and the migration map from today's files.
- `designs/*.dc.html`: one mockup per page. They're static HTML; open them for layout and copy. Their numbers are sample data.
- `designs/console.css`: the stylesheet every mockup uses. It's the source of truth for tokens and primitives.
- `assets/radisys-mark.png` and `assets/radisys-wordmark.png`: logos the user supplied, at low resolution. Replace them with official SVGs when available.

## 1. Tokens and theme (`src/styles.css`)

Replace the `:root` block with the "Signal" tokens from `designs/console.css`. The theme is dark-first:

| Group | Tokens |
| --- | --- |
| Grounds | `--ground #0b0f14`, `--rail #0f141b`, `--surface #141a22`, `--raised #1a222c`, `--hover #202a36` |
| Lines | `--line #26303c`, `--line-strong #364352` |
| Text | `--text #e6ecf2`, `--muted #9aa6b2`, `--faint #7f8b99` |
| Accent | `--volt #c8f051`, text on it `--volt-ink #0b0f14`, `--volt-soft` |
| Status | `--ok #3ccb8b`, `--warn #f0b43c`, `--bad #ff5d52`, `--info #6cb2ff`, each with a `-soft` fill |
| Severity | `--sev-critical #ff5d52`, `--sev-major #ff8a3d`, `--sev-minor #f0c43c`, `--sev-warning #6cb2ff`, `--sev-cleared #8a96a3`; text on them `--on-sev #0b0f14` |
| Radii | `--r-sm 6px`, `--r-md 10px`, `--r-lg 14px` |
| Fonts | `--f-display` Space Grotesk, `--f-body` IBM Plex Sans, `--f-mono` JetBrains Mono |

- Load the fonts from Google Fonts in `index.html`, or self-host them under `public/fonts`.
- Keep a light theme possible: put the values under `:root[data-theme="dark"]` and default to dark. A light palette wasn't designed, so ask the user before adding one.
- `scripts/gui_e2e.py` (axe) checks contrast. Every text-on-ground pair in the mockups is meant to pass WCAG AA; keep that check green.

## 2. Shell (`components/Layout.tsx`)

- **Sidebar, 248px** (see `Sidebar.dc.html`):
  - Header: the Radisys mark (36px, rounded) next to "AI-RAN SMO" with the subtitle "Radisys · Operator Console".
  - Below it, an environment chip ("lab-01 · Phase 1", plus healthy/total modules from `/health`).
  - Grouped nav: Overview (Dashboard, Lifecycle flows); Automation (rApps, Approvals, Decisions, Safeguards, AI/ML, Intents); Network (Alarms, KPIs & Assurance, Infrastructure, Data & Exposure); Account (Account security, Admin).
  - Replace the Unicode glyph icons with inline stroke SVGs (24px viewBox, 1.7 stroke); the path data is in `Sidebar.dc.html`. Count badges: open alarms (red) and pending approvals (amber).
  - Active item: `--volt-soft` background, volt text and a 3px volt inset bar.
  - Footer: the user card with role, MFA state and sign-out. Keep pinned rApps under "rApps".
- **Top bar, 64px** (see `Topbar.dc.html`): breadcrumb (section / page); global search with a ⌘K hint (it can start as a client-side jump to pages, rApps and alarm ids); a live/refresh chip; an autonomy chip; a notifications button with a count; help.
- Below 800px the sidebar stacks above the content, as today.

## 3. New shared primitives (add to `components/ui.tsx` or a new `components/kit.tsx`)

Each maps to a class in `console.css`:

- `Kpi` (`.kpi`, `.kpi-l`, `.kpi-v`, `.kpi-f`; variants `hot`, `volt`) replaces `.stat` and drops the left-border accent.
- `Meter`, a stacked bar: `.meter > span.f-*`.
- `Segmented` (`.seg`): time range and autonomy mode.
- `Badge` (`.badge` plus `b-ok|warn|bad|info|mute|volt`; `plain` hides the dot). `StateBadge` keeps its `TONES` table and maps it onto these classes.
- `SeverityChip` (`.sev-cr|mj|mn|wn|cl`).
- `Timeline` (`.tl`, `.tl-i.done|now|fail`) and `Steps` (`.steps .step.done|now`); extend `FsmStepper` with them.
- `Callout` (`.callout` plus `bad|warn|volt`) for attention items and root-cause hints.
- `Diff` (`.diff` with `.m`/`.p` lines) for config changes awaiting approval.
- `Tabs`: restyle, and add a count pill (`.tab .n`).
- `DataTable`: restyle (mono uppercase headers on `--rail`, selected row with a volt inset bar).
- Charts: restyle `Sparkline`, `CountBar` and `SeriesChart` with `.ln-*`, `.ar-*` and `.floor`. Add a stacked bar chart (alarms per hour) and a ring gauge (intent fulfilment).

## 4. Page by page

Each page keeps its current route, data hooks and actions. "New" below means a new presentation of data that already exists, unless it's flagged ⚠ (see section 5).

| Page | File | Changes |
| --- | --- | --- |
| Sign in | `pages/Login.tsx` | Split screen: brand panel (headline, sector-ring graphic, "A product of Radisys" wordmark on a white chip) and form (SSO button first, local account, MFA note). |
| Dashboard | `pages/Dashboard.tsx` | 5 KPI tiles (network health ⚠, open alarms with severity meter, autonomous actions in 24 h, awaiting approval, model guard breaches). **Network health map**: site clusters as tiles, coloured by the worst DU state and drillable region → cluster → DU (⚠ needs a region/cluster on each DU), next to a server-ranked "Worst 10 DUs" list. All counts come from one summary call (see SCALE.md). "Needs your attention" list built from critical alarms, pending approvals, MLMF breaches and SA SMOS escalations. Alarms-per-hour stacked bars. Autonomy activity feed (decisions). Module health as a compact tile grid. AI/ML pipeline counts. Fleet summary. |
| Lifecycle flows | `pages/Flows.tsx` | Flow list with a progress bar per flow. Selected flow: subject picker, module chips, a **sequence lane** (actors as columns, calls as arrows, current one dashed) built from `FlowStep.actor`, and the existing timeline restyled. |
| rApps | `pages/Rapps.tsx` | Tabs: Instances, Packages, Directory & pages. Instance cards (icon, version, state, autonomy segmented control, headline KPI with sparkline ⚠ per-rApp KPI, actions in 24 h, last decision, safeguard summary). All-instances table with a health meter. Package pipeline and upgrade-in-progress strip. |
| rApp detail | `pages/RappDetail.tsx` | **Lifecycle flows for this rApp**: flows 01, 06, 07 and its model's flow 02, as steppers from `lib/flows.ts`, with a paged lifecycle history and an Upgrade action. The rApps table gets a matching lifecycle column. Header with autonomy segmented control, Pin and **Stop rApp**. KPI tiles. **Cell-state swimlane** (SERVING / PRE_SLEEP / SLEEP / LOCK over 24 h ⚠ history). Recent decisions table, safeguard meters, lifecycle timeline, reported KPIs chart, faults. Declared operator pages still render below. |
| Approvals | `pages/Approvals.tsx` | Master–detail. Queue cards with a lapse countdown bar. Detail: title, countdown, expected impact tiles, **config diff**, safeguard check badges, "Why the rApp asks" with input chips, decision box (reason, Approve & write, Reject). ⚠ The "approve similar for 1 hour" checkbox is a proposal; leave it out unless the backend adds it. |
| Decisions | `pages/Decisions.tsx` | Filter bar, 4 KPI tiles, table with a selected row. Detail panel as a 6-step chain: inputs → model → rationale → config job → approval → verify. Integrity box (hash and previous hash, "chain intact"). |
| Safeguards | `pages/Safeguards.tsx` | "Stop all rApp writes" button ⚠ (or loop the per-rApp stop). Per-rApp cards with Stop/Resume, limit meters (change per write, jobs per hour used vs. max, elements per job), "hold for approval" toggle, refusal count. Refusals table with a reason badge. Watchers list. |
| AI/ML | `pages/Aiml.tsx` | **Kanban** of models by stage (Registered, Training, Validating/emulating, Promoted, Active runtime), with any model under its floor outlined red. Model detail: `MODEL_PIPELINE` steps, guard-KPI chart with a dashed floor line, metric tiles. Side cards: training job progress ⚠ epoch/ETA, waiting for governance, last inference jobs. |
| Intents | `pages/Policy.tsx` | Intent cards with a fulfilment ring ⚠ percent (derive from intent reports), expectation box, handler, conflict callout. A "New intent" form with handler-support and overlap checks. |
| Alarms | `pages/Alarms.tsx` | Severity tiles act as filters, plus mean time to acknowledge ⚠. RAN / O-Cloud tabs in the table card, with a 24 h sparkline. Detail panel: Ack/Clear/Assign, **likely root-cause callout** ⚠ (correlation; at first, the same managed element within N seconds), TS 28.532 fields, lifecycle timeline. |
| KPIs & Assurance | `pages/Kpis.tsx` (+ `KpiDefinitions.tsx`) | Overview tab first: 5 KPI tiles with sparklines and a per-DU throughput chart with the incident shaded. Assurance monitors table (floor vs. now, remedial action, state). "Escalated to you" callout. RAN Analytics reports. The existing tabs stay. |
| Infrastructure | `pages/Infrastructure.tsx` | New default **Topology** tab: a graph of O-Cloud → deployment manager → pool → resources → workloads, built from TEIV entities and relationships, coloured by health. Inspector panel for the selected node (attributes, utilisation ⚠, what runs on it). Service-order cards with an INFRA → TRAINING → DEPLOY stepper. |
| Data & Exposure | `pages/Data.tsx` | **Data-flow view**: producers → data types → consumers, with band width = job count, built from DME types, jobs and offers. Data jobs table with last delivery and a LATE state ⚠. SME exposed services list. |
| Account security | `pages/Security.tsx` | 3 status tiles, re-enrol authenticator, recovery codes grid (used codes struck through), recent sign-ins ⚠. |
| Admin | `pages/Admin.tsx` | Users table (avatar, role badge, sign-in method, MFA state, last active ⚠), role matrix card, audit log with filters and an HTTP outcome badge. |

## 4b. Lifecycle flows 01–10, tabs and clicks

- **One board per flow:** `Flows.dc.html` (01) and `Flow02`…`Flow10.dc.html`. There's no flow 05 in `smo/docs/call-flows`. Each flow has a route `/flows/:id` with four sections: a fleet funnel (counts per step, server-side), a subject picker (typeahead), a sequence diagram (actors from `FlowStep.actor`), and the step timeline with that flow's actions. The step titles and actors match `lib/flows.ts` exactly. Flows 11–27 are documented but not tracked yet; adding them is a catalogue entry plus an evaluator in `lib/flows.ts`.
- **Every tab is designed and switches when clicked.** The tab id is kept in the URL hash, which already works through `useHashTab`:
  - rApps: Instances, Packages, Rollouts, Directory
  - AI/ML: Models, Training, Inference, Feature groups, Coordination, MLMF
  - Alarms: RAN, O-Cloud, FM subscriptions
  - KPIs: Overview, PM, Definitions, rApp performance, RAN Analytics, Assurance, O-Cloud
  - Intents: Intents, Handlers, Dispatches
  - Infrastructure: Topology, NF deployments, O-Cloud inventory, O1 endpoints & jobs, Service orders
  - Data: flow & jobs, producers & offers, SME
  - Safeguards: Limits, Refusals, Watchers
  - Admin: Users, Audit log
- **Lists:** non-overview tabs use one shared server list (`ListView.dc.html` → `kit/ServerTable`). The columns are the repo's own table headers.
- **Clicks wired in the prototype** (implement the same behaviour):
  - Sidebar and flow-list navigation.
  - Dashboard cluster drill-down, with Back.
  - Alarms: severity tiles toggle the filter; group rows expand.
  - Decisions: selecting a row updates the detail panel.
  - Approvals: approve / reject / approve-group show the result, with Undo.
  - rApp detail: autonomy switch, pin, and Stop (confirm dialog) / Resume. Lifecycle rows open their flow board.
  - Admin: the Add user dialog.
  - Table name links open their detail board.

## 4c. Flows in scope (final)

The flows are generic only: 01, 02, 03, 04, 06, 07, 08, 09, 10, **15** (NFO workload: instantiate → scale → heal → terminate), **16** (FOCOM resource: subscribe → provision → notify → deprovision) and **19** (RAN software job: download → install → activate, with a failed phase). Flow 02 now also covers **17** (runtime scale/terminate) and **26** (promote, rollback, deprecate, retire), shown as three phases: Build & certify, Serve, End of life.

These are left out on purpose: 22–25 (sample-rApp closed loops) and 12, 13, 14, 18, 20, 27 (explanatory, or a detail of another flow). Flows 21 (O1 vendor onboarding) and 11 (DME producer/type) are parked for later.

## 4d. User preferences (theme, text size, accent): per user, on every page

- **Settings:**
  - theme: `dark` | `light` | `system`;
  - text size: `s` 90% | `m` 100% | `l` 112% | `xl` 125%;
  - accent: `volt` (default) | `blue` | `teal` | `amber` | `radisys` (#df1f4e);
  - start page, rows per page, time zone, 12/24-hour time, reduce motion, sound for critical alarms.
- **Storage:** with the user in the GUI BFF: `GET/PUT /api/me/preferences`, validated against the enums above. The same table holds users today, so SSO users get a row on first sign-in. The browser also keeps a copy (`localStorage["smo.prefs"]`) only so that the first paint is already right.
- **Applying:** a `ThemeProvider` in `shell/` sets `data-theme`, `data-accent` and the text scale on `<html>`. Implement the scale as the root `font-size` with rem units in the real CSS; the mockups use `zoom` only as a shortcut. A small inline script in `index.html` applies the cached copy before React mounts, so there's no flash of the wrong theme. "System" listens to `prefers-color-scheme`.
- **Tokens:** `console.css` now defines both themes and all five accents. Every page uses tokens only: no hard-coded hex for text, surfaces or the accent. The accent has four tokens: fill, text-on-fill, text-on-ground and soft, with separate light and dark values so links stay at ≥ 4.5:1. Status and severity colours don't change with the accent, and in light mode they're darkened for white backgrounds.
- **UI:** a Preferences page (sidebar → Account), with live preview and Save/Reset, plus a moon icon in the top bar that opens it. Unsaved changes preview only on that page until saved.
- **Tests:** axe contrast checks run on both themes × all accents in `scripts/gui_e2e.py`.

## 4e. Backend capabilities brought into the GUI (features 1–11)

Each of these is an existing backend route that no page used before. The board is named in brackets.

| # | Where | What to build | Backend routes |
| --- | --- | --- | --- |
| 1 | **New page: RAN topology** [Topology] | Relation counts (not reciprocal, external, ambiguous), a focus-one-element neighbour graph (dashed = one-way), a relation checker, and a "relations that need attention" table that links to cell guards. The Dashboard's "Open topology" link goes here. | `GET /ran-nf-oam/topology`, `/topology/links?managed_element_ref&link_type` (INTRA_ELEMENT, INTER_ELEMENT, AMBIGUOUS, EXTERNAL; `reciprocal`, `sameSectorGroup`, `sameIncidentZone`), `/topology/relation?a&b` |
| 2 | **New page: Software** [Software] | Campaign list; campaign detail with waves, gate and halted reason (GATE_FAILED, WAVE_PAUSE, OPERATOR_HALT); Continue, Roll back, Abort; an event log; elements per wave (each linking to flow 19); and a new-campaign form with a dry run. | `/software-campaigns` (+ `/{id}`, `/continue`, `/halt`, `/abort`, `/rollback`); states PENDING, RUNNING, HALTED, COMPLETED, ABORTED, ROLLING_BACK, ROLLED_BACK, ROLLBACK_FAILED; body: selector (vendorName, region, entityType, tenant), waveSize, wavePauseSeconds, gateMaxNewAlarms, onGateFailure (halt or rollback), dryRun |
| 3 | **New page: Configuration** → Config jobs [Configuration] | Staged-job detail: waves, pause countdown, Continue now, Halt, Run KPI check, Roll back, Abort; the KPI guard result; sub-change counts. Also a new-job form (waves, gate, on gate failure halt or revert, KPI guard, dry run). | `/config-jobs` (+ `/halt`, `/continue`, `/abort`, `/kpi-check`, `/rollback`); JobState PENDING, PROCESSING, HALTED, COMPLETED, PARTIAL_SUCCESS, FAILED; `haltedReason`, `currentWave`, `waveCount` |
| 4 | **New page: Element detail** [Element] | Overview; config history (pick two snapshots → diff → roll back); a managed-object tree with attributes and refresh; cell guards editor (class NORMAL, COVERAGE_CRITICAL or EMERGENCY; sector group; incident zone; neighbours). Linked from Topology, Worst DUs and the Dashboard drill-down. | `/managed-entities/{me}/config-history`, `/config-history/diff`, `/managed-objects/{dn}`, `/children`, `/subtree`, `/managed-objects/refresh`, `PUT /managed-entities/{me}/cells/{cell}/guards`, `/cell-guards` |
| 5 | Admin → **RAN access control (MSAC)** tab | Roles, identities (USERNAME, EMAIL_ADDRESS, PHONE_NUMBER, IP_ADDRESS, MACHINEUSER) and access rules (ALLOW or DENY, operations, data-node selector) | `/ran-nf-oam/msac/roles`, `/identities`, `/access-rules` |
| 6 | Infrastructure → O-Cloud inventory | A level picker: locations → sites → node clusters → cluster resources → infrastructure resources, plus provisioning requests and performance jobs | `/focom/locations`, `/o-cloud-sites`, `/node-clusters`, `/cluster-resources`, `/infrastructure-resources`, `/provisioning-requests`, `/performance-jobs` |
| 7 | AI/ML → model detail | Governance and lifecycle history (decision, from → to, decided by, rationale); Roll back, Deprecate and Retire with a rationale; Suspend/Resume on training jobs | `/aimgf/models/{id}/governance-history`, `/lifecycle-history?fsm=`, `/training-jobs/{id}/suspend`, `/resume` |
| 8 | AI/ML → **Registry** tab | Repositories, storages, artifact versions | `/aimgf/ml-model-repositories`, `/storages`, `/models/{id}/artifact/{version}` |
| 9 | KPIs → RAN Analytics | Request-an-analysis form (function, output, scope, ANALYTICS / PREDICTION / DRIFT, FILE / STREAMING / NOTIFICATION), MDA functions, requests, report file download | `/mdaf/mda-requests`, `/mda-functions`, `/mda-reports/{id}/file` |
| 10 | Intents | A **Utility formulas** tab; negotiation feedback (satisfaction) on each intent outcome | `/intent-service/intent-utility-formulas`, `POST /intents/{id}/negotiation-feedback` |
| 11 | Configuration → Vendors & schemas, Endpoint trust, Element onboarding | Vendor capabilities (conformance SPEC, OWN or COMBINED; modes; schema refs), CM schemas (YANG, OPENAPI_NRM or DESCRIPTOR), pinned SSH host keys with a mismatch alert and re-pin, element onboarding (discovered → selected → applied, with a software check) | `/vendor-capabilities`, `/cm-schemas`, `/o1-adaptor-endpoints/{id}/host-keys`, `/element-onboarding` (+ `/select`, `/apply`) |

The sidebar's Network group now has: Alarms, KPIs & Assurance, **RAN topology**, **Configuration**, **Software**, Infrastructure, Data & Exposure. Every new list uses the server table (P1); the new tiles need summary counts (P2).

## 5. Data gaps (⚠): confirm with the user before adding endpoints

Network health score; DU↔cell topology for the RAN mini-map; per-rApp headline KPI and cell-state history; training epoch/ETA; intent fulfilment percent; mean time to acknowledge; alarm root-cause correlation; node utilisation; DME "late" detection; sign-in history; user last-active; global "stop all"; "approve similar for N minutes". Until each gap is filled, hide its widget or show "—".

## 6. Suggested order of work

0. The scale fixes from SCALE.md (P1 server tables, P2 summaries, P8 targeted invalidation, P10 states), together with the folder restructure from STRUCTURE.md, one page at a time.
1. Tokens, fonts, `console.css` primitives merged into `styles.css`; fix contrast.
2. Shell: sidebar, top bar, icons, logo.
3. Shared primitives and chart restyle.
4. Pages: Dashboard, Alarms, Approvals, rApps and rApp detail, then the rest.
5. Update the tests that assert old class names or markup, re-run vitest and the axe e2e, and refresh the screenshots in `docs/screenshots`.

Don't push to `main`. Work on a branch and open a PR.
