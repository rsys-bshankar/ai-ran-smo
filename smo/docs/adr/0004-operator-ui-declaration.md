# ADR 0004 — The operator page a rApp declares (`operatorUi`)

Status: accepted (`PR-GUI-8`, steps `GUI-8.1`, `GUI-8.2`, `GUI-8.8`; built in `GUI-8.3` to `GUI-8.6`). Date: 2026-10-08. Release 0.6.0.

## Context

The operator GUI has a fixed sidebar (`gui/src/components/Layout.tsx`) and one hand-written page for each sample rApp
(`gui/src/pages/{EnergySaving,Mobility,Coverage,TrafficSteering}.tsx`), plus a static entry for each in the gateway's route table
(`r1-termination/app/main.py`) and in the GUI backend's permission table (`gui-bff/app/rbac.py`). That does not scale to 100 rApps, and a rApp
onboarded at run time shows only on the generic rApps page.

Decided with the owner: the sidebar keeps **one** rApps entry; a directory lists every rApp; **what a rApp shows on its page is declared in
its package and drawn by one generic renderer in the GUI**, so onboarding the rApp makes its page appear without a GUI build. The rApp's
declared routes are reached through the GUI backend, with permissions derived from the declaration. This record fixes the declaration, its
limits, how the rApp's operator API is reached, and what the check at onboarding refuses. The GUI backend, the directory and the renderer
are `PR-GUI-8`'s later steps (built in `HISTORY.md` PR-GUI-8b; the browser check, `GUI-8.7`, is open in `OPEN_ITEMS.md`).

**Not taken:** a rApp shipping its own JavaScript or an iframe. Code from a package would run in the operator's browser session with the
operator's rights; reviewing it, sandboxing it and supporting it across GUI releases is a cost a declarative format does not have. After
1.0.0, if ever.

## Decision

### 1. Where the declaration lives: a key of `manifest.yaml`

`operatorUi` is a key of `manifest.yaml`, next to `executionModes`, `runtimeProfiles` and `limits`: under `rappManifest`, or at the top level
of the file (the layout the sample rApps use for those keys). Onboarding already parses that file, stores its AI-platform part as the
package's `aiCapabilities` and returns it from `GET /packages` and `GET /packages/{id}/onboarding-status`, which is the route rApp Management and AIMgF
already read. So:

- no new file in the CSAR, no new reader, **no new column or migration**, no change to the OpenAPI document (`aiCapabilities` is an open
  object); the declaration is `aiCapabilities.operatorUi`, and a package without it has no such key;
- the other choice, a separate `ui/operator-ui.json`, would be a second file to find, parse and size-limit, a new column or a new route to
  hand it to the GUI backend, and a second place for an author to forget. It would be the better choice if declarations grew large (they are
  limited to 64 KiB, see 5) or if non-YAML tooling had to write them; neither holds. If either does, the key can later accept
  `operatorUi: {file: ui/operator-ui.json}` without breaking a package.

A YAML author must quote a route that contains `{...}` (`path: "/instances/{instanceId}"`): a bare `{` starts a flow mapping.

### 2. The declaration

```yaml
operatorUi:
  version: 1              # required; the SMO refuses a version it does not know
  readOnly: false         # optional; true: the rApp declares no change (see 7)
  panels: [ ... ]         # required; ordered, 1 to 20, drawn top to bottom
```

Every panel has `id` (`[a-z][a-z0-9-]*`, at most 40, unique in the declaration), `title` (at most 80), `kind` and, except for a `kpis` panel
whose tiles all name a KPI and an `actions` panel, a `source`.

**`source`** is the route that supplies the panel's data: `path` (a route relative to the rApp's operator API base, see 4), optional `query`
(at most 8 parameters with fixed string, number or boolean values), optional `refreshSeconds` (5 to 3600; absent: read when the page opens and
on a manual refresh). A source is a GET. `method` may be written, and only `GET` is accepted.

| Kind | Fields | Draws |
|---|---|---|
| `table` | `rows` (field path of the list in the answer; omitted: the answer is the list), `rowKey` (field of a row that identifies it), `columns` (1 to 20), optional `rowActions` (at most 5), optional `rowDetail` (the drawer of a row, below), `empty` (text for no rows) | A table, one line per element; a click on a line opens its drawer. |
| `keyValues` | `items` (1 to 30 of `label`, `path`, `format`, `unit`) | A two-column list of labelled values from one object. |
| `kpis` | `tiles` (1 to 12 of `label` and one of `path` or `kpi`, `format`, `unit`) | Number tiles. `path` reads a number from the source's answer. `kpi` names a KPI the platform holds for the instance (the performance reports of rApp Management); such a tile needs no source, and the panel has none when every tile is bound by `kpi`. |
| `chart` | `type` (`line` or `bar`), `points` (field path of a list), `x`, `y` (fields of each point), optional `seriesBy` (field that splits the points into series, at most 8 drawn), `unit` | A chart of one numeric series per `seriesBy` value, `x` being a time or a label. |
| `actions` | `actions` (1 to 10) | A row of buttons, with an optional `source` the buttons' state may be shown from. |

**Column** (`table`): `path` (relative to the row), `label`, optional `format`, `unit`, and for `format: sparkline` the field `y` of each point
(`path` then names the list of points). **Formats:** `text` (default), `number`, `percent`, `datetime`, `badge` (a state word drawn in the
state colours the GUI already uses), `id` (short, monospaced, copyable), `boolean`, `list` (an array joined with commas), `sparkline` (table
columns only). A value that does not fit its format is shown as text.

**Action** (a button): `id` (unique in the whole declaration: the audit names it), `label`, `method` (`POST`, `PUT`, `PATCH`, `DELETE`),
`path`, `success` (text shown when the call succeeded), optional `confirm` (text of a confirmation dialog; absent: no dialog), `tone`
(`default`, `primary`, `danger`), `role` (`operator`, the default, or `admin`: who may press it; an admin-only button is not drawn for anyone
else, and the GUI backend refuses it to them), `inputs` (at most 8 fields the button asks for before it sends), `body` (fixed values, at most 8). An input is
`name`, `label`, `type` (`string`, `integer`, `number`, `boolean`, `enum`), optional `required`, `options` (an enum's values, at most 50), `min`,
`max` (numbers), `maxLength` (strings). The request body is a JSON object made of the inputs and the fixed `body`, nothing else; a name is
either an input or fixed. The fixed string `"{user}"` is replaced by the **GUI backend** with the signed-in user, never with anything the
browser sent, so a rApp can attribute an override to the real operator. A `DELETE` has no body. A row action also has `when`
(`{path, exists}`, `{path, equals}` or `{path, notEquals}` on a field of the row) to show the button only for some rows, such as "Override"
for a cell with none and "Clear override" for one with it.

**`rowDetail`** (a field of a `table`) is what a click on a row opens: a drawer with an optional `title` (text, which may use `{row.<field>}`) and
1 to 6 `blocks`, drawn in order. It was first proposed as a version 2 feature, because the first draft of this record left it out; the owner decided
to build it before the sample pages are migrated, and since nothing of version 1 has been released it is part of version 1 (`version: 1` is unchanged).
A block has a `kind` and a `title` (at most 80), and is one of:

| Block | Fields | Draws |
|---|---|---|
| `json` | optional `path` (field path of a sub-object of the row; omitted: the row itself), optional `empty` (text when it is missing) | The value as formatted text (indented JSON, in a monospaced box). |
| `keyValues` | `items` as in a key-values panel, read from the row | Labelled values of the row. |
| `table` | `columns` as in a table, and one of `rows` (a list field of the row) or a per-row `source` (with `rows` then picking the list in its answer), optional `empty` | A table of a list the row carries, or of a list fetched for this row. |
| `chart` | `type`, `points`, `x`, `y`, `seriesBy`, `unit` as in a chart panel, and optionally a per-row `source`; without one, `points` is a list field of the row | A chart of the row's own series, or of a fetched one. |

A per-row `source` is a GET like every source; its route may use `{instanceId}` and `{row.<field>}`, and a query value may be the whole string
`{row.<field>}` (`query: {cell_id: "{row.cellId}"}`), nothing else with braces. A `{row.<field>}` anywhere (a detail source, the drawer title, and
equally a row action's route) must name the table's `rowKey` or the `path` of one of its columns: a field the page itself shows, so an author cannot
refer to a field the table does not carry. A block cannot hold a `rowDetail` (no nesting), and the table's own buttons stay in the table
(`rowActions`), not in the drawer. The drawer reads the row the table already has; a fetched block is requested when the drawer opens and when
its `refreshSeconds` (if any) elapses, and only for the open row.

**Path parameters.** A route template may contain `{instanceId}`: the GUI substitutes the id of the rApp instance whose page is open (the
instance id rApp Management assigned; the sample rApps already use it as their own instance id). In a table's row actions it may also contain
`{row.<field>}`: the value of that field of the row the button belongs to. Nothing else in braces is accepted. A substituted value must be
made of letters, digits and `._~-`, and not be `.` or `..`; otherwise the GUI does not send the call.

**Field paths** (the JSON-path subset): dotted names of letters, digits, `_` and `-` (`latestDecision.prediction.model.futurePrb`), with at most
one `[]` that walks every element of a list (`datasets[].dataset`, drawn as a list). That is all: no `$`, no index, no wildcard, no filter, no
`..`, at most 8 names and 100 characters. A name missing from the answer makes the cell show "—"; it is not an error. `rows`, `points`,
`rowKey`, `x`, `y`, `seriesBy`, a tile's `path` and `when.path` take no `[]`.

### 3. How text is escaped

Everything the declaration or the rApp's answers put on the page is **text**. The renderer builds text nodes, never HTML and never markdown;
a title of `<script>` is drawn as the characters `<script>`. A value is never turned into a link, an image, a style or an attribute that
means something (a `badge`'s colour comes from the GUI's table of state words, not from the value). The declaration's strings may not contain
control characters (Onboarding refuses them) and are length-limited; their content is otherwise not filtered, because it is not interpreted.

### 4. How the rApp's operator API is reached

Two ways were weighed:

- **(a) A base URL the instance registers.** The rApp instance gives rApp Management an `operatorApiBase` (a new nullable field of the rApp
  instance, set by the rApp at bootstrap through its own authenticated call, and by the operator in the instance configuration for a rApp that
  cannot call). The GUI backend reaches the declared routes **through the R1 gateway**, which resolves `…/rapps/{instanceId}/operator/<route>` to
  that instance's base, as it resolves a registered SME provider's API today: the gateway's token check stays the one door, and the call is
  counted, traced and rate-limited like any other.
- **(b) Static gateway routes** (today's `/energy-saving-rapp`, … entries). The rApp's address is fixed in the gateway's table and in the
  deployment environment (`ENERGY_SAVING_RAPP_URL`), so a rApp onboarded at run time has no route until the gateway is changed and restarted.
  That defeats the purpose.

**Chosen: (a).** No decisive problem was found; the ones that exist are handled:

- The base URL is supplied by a workload and then called by the platform, which is the SSRF shape `smo_shared.webhook` exists for. The base is
  accepted only from the instance's own authenticated call (or from an operator), must be `http` or `https`, and passes the same
  `is_safe_webhook_destination` check (no loopback, link-local or metadata addresses) before it is stored and again before each call. A
  hostname that resolves to a blocked address is the accepted residual risk that module's docstring records.
- The gateway has a static routing table today. Stage 2 gives it one dynamic prefix whose target comes from rApp Management (cached for a short
  time, dropped when the instance is terminated); the existing static routes stay for the four samples until `GUI-8.6` removes them.
- A rApp that has no `operatorApiBase` simply has no declared panels working: the page shows the generic overview and "this rApp's operator API is
  not registered".
- The browser never calls the rApp, and never sees its address.

The rApp's operator API is the rApp's own HTTP API for operators. The declaration says which of its routes the GUI may use; it does not
describe or constrain the rest of that API (the route `…/sim-producer/publish` of the Energy Saving rApp stays a machine route).

### 5. Limits

Onboarding refuses a package over any of these (`smo_shared/operator_ui.py`, constants of the same names):

| Limit | Value |
|---|---|
| Declaration as compact JSON | 65 536 bytes |
| Values (objects, lists, scalars) in it | 4 000 (this also stops a YAML alias bomb before anything expands it) |
| Nesting | 10 levels |
| Panels per rApp | 20 |
| Columns per table / row actions per table / blocks per `rowDetail` | 20 / 5 / 6 |
| Items per key-values panel / tiles per KPI panel | 30 / 12 |
| Actions per actions panel | 10 |
| Inputs, fixed body values, query parameters, per action or source | 8 |
| Options of an enum input | 50 |
| Route template / field path | 200 / 100 characters |
| `refreshSeconds` | 5 to 3600 |
| Title / label / confirm text / success text | 80 / 60 / 300 / 200 characters |

### 6. Versions, unknown things and extension keys

- `version` is a whole number. This SMO accepts `1`. Onboarding refuses another value (`operatorUi.version: 2 is not supported by this SMO`),
  because a package written for a newer format would be drawn wrongly, and a clear refusal at onboarding is cheaper than a broken page.
  A new panel kind, format or field is a new version; the SMO that adds it accepts both.
- **Unknown keys are refused** at onboarding (a typo in `refreshSecond` must not pass silently), **except keys starting with `x-`**, which are
  ignored at every level and dropped from the stored declaration. Authors use them for their own notes or tooling.
- **Unknown kinds:** Onboarding refuses a kind that is not one of version 1's. The GUI is separately defensive, because the stored
  declaration may come from a newer Onboarding than the GUI build, or be edited in the database: a panel whose `kind` the renderer does not know is
  drawn as a card with its `title` and "unsupported panel", the other panels are drawn, and nothing throws. The same holds for an unknown `format`
  (drawn as text) and an unknown column or tile shape (the panel is "unsupported"). A `rowDetail` block whose `kind` the renderer does not know is
  drawn as "unsupported block" with its `title`, and the drawer's other blocks are drawn.

### 7. Permission: exactly the declared routes

The GUI backend forwards a call for rApp instance *X* only if `(method, path)` is one of the **declared routes** of *X*'s package: the union of
every panel's `source` (all `GET`), every action's route (`actions` panels and `rowActions`) and every `rowDetail` block's per-row `source`
(also `GET`, so reading a drawer needs viewer), with `{instanceId}` set to *X* and `{row.<field>}`
matching one safe segment, and nothing else. `smo_shared.operator_ui.declared_routes` computes the set and `route_allowed` is the match
(it refuses `..`, `%`, `//`, `?`, `#` and any segment outside `A-Za-z0-9._~-`). Then:

| Call | Role needed | If not declared |
|---|---|---|
| a declared read (`GET`) | viewer | refused (`403 UNDECLARED_ROUTE`), whatever the role |
| a declared change (`POST`, `PUT`, `PATCH`, `DELETE`) | operator | refused the same way |
| a declared change whose action says `role: admin` | admin (`403 FORBIDDEN` for an operator) | refused the same way |
| any change on a rApp whose declaration has `readOnly: true` | nobody | refused (`403 RAPP_READ_ONLY`) |

`readOnly: true` is the rApp saying "this page only shows"; Onboarding refuses a declaration that is `readOnly` and also declares an action,
so the flag cannot be a lie. A request body is checked against the action: only the declared inputs and fixed values, with the declared types and
bounds; extra fields are dropped, a missing required one is `422`. The query string of a read is the declared `query` only. **Every change is
audited** (who, rApp instance, action id, route, outcome) in the GUI backend's audit record, before the call is sent and again with the
outcome. The rApp's own authorisation still applies to what reaches it; the declaration narrows what the GUI will ask, it does not widen what
the rApp accepts.

The check is on the declaration stored with the instance's package, read from Onboarding, never on anything the rApp's answers say.

### 8. What Onboarding refuses (`GUI-8.2`)

Onboarding validates `operatorUi` with `smo_shared.operator_ui.validate_operator_ui` when it reads `manifest.yaml`. A refusal ends the package in
`FAILED` as every other validation failure does (`docs/RAPP_PACKAGING.md` §6), logs the message, and returns it in the `failureReason` of the
`202` answer to `POST /packages` (the reason is not stored; a package that is `FAILED` is onboarded again once fixed). The message names the
place and the rule, for instance `operatorUi.panels[2].columns[1].path: must not contain '..'`. Refused:

- an unknown or missing `kind`, an unknown key, a version other than 1;
- a `source` (a panel's or a `rowDetail` block's) that is not a GET, an action that is a GET or any method outside `POST`, `PUT`, `PATCH`, `DELETE`;
- a route that does not start with `/`, is over 200 characters, contains `..`, `.` or an empty segment, `%`, `?`, `#`, a space, or a `{...}` other
  than `{instanceId}` (and `{row.<field>}` in a row action);
- a field path outside the subset (including `..`, `$`, an index, two `[]`);
- more panels, columns, items, tiles, actions, inputs, options or query parameters than the limits; a declaration over the byte or value limit;
- a duplicate panel id, a duplicate action id (across the whole declaration), two inputs of one name, a name that is both an input and a fixed value;
- a `rowDetail` with an unknown block kind, with no blocks or more than 6, nested in a block, a `table` block with neither `rows` nor a `source`, braces in a
  query value other than a whole `{row.<field>}`, and a `{row.<field>}` (in a detail source, the drawer title or a row action) that names no column or `rowKey`;
- `readOnly` with an action; a `sparkline` column without `y`, or `y` without `sparkline`; a tile with both or neither of `path` and `kpi`; a tile
  `path` with no panel `source`;
- strings with control characters or over their length; values that are not JSON (YAML dates, `.nan`).

The manifest has no list of the rApp's API routes to check an action against, and none is added here: the declared routes *are* the rApp's
operator-API surface as far as the SMO is concerned (7). If a package later carries an OpenAPI description of that API, Onboarding can refuse
a declared route the description does not contain; that is an additive check.

The published JSON Schema (`docs/schemas/operator-ui-1.schema.json`, generated from the same constants) states the structure and the numeric
limits for tools outside the SMO. The rules that relate two places (duplicates, `readOnly`, the byte limit) are in the validator only, which
is the authority.

### 9. Authoring

`smo_sdk.operator_ui` (`sdk/README.md`) builds a declaration with functions (`table`, `key_values`, `kpis`, `chart`, `actions`, `action`, …),
validates it with the same code, and appends it to a `manifest.yaml`. `sdk/examples/hello_operator_ui.py` is the smallest package that uses it.

## Worked example: the Energy Saving page

`gui/src/pages/EnergySaving.tsx` shows, for one instance: the instance (`GET /instances/{id}`: managed element, autonomy mode, actuator, model),
the two buttons "Evaluate now" and "Reconcile approvals", the cells table (`GET /instances/{id}/dashboard`, `cells`: state, O1 value, PRB trend,
prediction, decision, outcome) with a per-cell "Override: unlock" / "Clear override", and, in a drawer opened by a click on a cell, the PRB chart, the latest execution as JSON
(`CellDrawer`) and that cell's last 20 decisions (`GET /instances/{id}/decisions?cell_id=…`). As a
declaration (the file `docs/schemas/operator-ui.energy-saving.example.yaml`; the Onboarding and shared tests load this file, and a test fails if
this text and the file differ):

```yaml
operatorUi:
  version: 1
  panels:
    - id: instance
      title: Instance
      kind: keyValues
      source:
        path: "/instances/{instanceId}"
        refreshSeconds: 30
      items:
        - {label: Managed element, path: managedElementRef}
        - {label: Autonomy mode, path: autonomyMode, format: badge}
        - {label: Actuator, path: actuator}
        - {label: Model, path: modelId, format: id}
        - {label: Model version, path: modelVersion, format: number}
    - id: controls
      title: Closed loop
      kind: actions
      actions:
        - id: evaluate
          label: Evaluate now
          tone: primary
          method: POST
          path: "/instances/{instanceId}/evaluate"
          success: Closed-loop pass complete
        - id: reconcile
          label: Reconcile approvals
          method: POST
          path: "/instances/{instanceId}/reconcile"
          success: ASSIST dispatches reconciled
    - id: cells
      title: Cells
      kind: table
      source:
        path: "/instances/{instanceId}/dashboard"
        query: {points: 48}
        refreshSeconds: 15
      rows: cells
      rowKey: cellId
      empty: No managed cells.
      columns:
        - {path: cellId, label: Cell}
        - {path: state, label: State, format: badge}
        - {path: overrideBy, label: Override}
        - {path: o1Value, label: O1}
        - {path: prbTrend, label: PRB trend, format: sparkline, y: v}
        - {path: latestDecision.prediction.model.futurePrb, label: Predicted PRB, format: percent}
        - {path: latestDecision.decision, label: Decision, format: badge}
        - {path: latestDecision.reason, label: Reason}
        - {path: latestDecision.outcome, label: Outcome, format: badge}
      rowActions:
        - id: unlock-cell
          label: "Override: unlock"
          tone: danger
          method: POST
          path: "/instances/{instanceId}/cells/{row.cellId}/override"
          confirm: Unlock this cell now and suppress AI recommendations for it?
          success: Cell unlocked by operator
          when: {path: overrideBy, exists: false}
          body: {operator: "{user}", reason: manual override}
        - id: clear-override
          label: Clear override
          method: DELETE
          path: "/instances/{instanceId}/cells/{row.cellId}/override"
          success: Override cleared
          when: {path: overrideBy, exists: true}
      rowDetail:
        title: "Cell {row.cellId}"
        blocks:
          - kind: chart
            title: PRB utilisation (%)
            type: line
            points: prbTrend
            x: t
            y: v
          - kind: json
            title: Latest execution (audit trail)
            path: latestDecision
            empty: No decision yet.
          - kind: table
            title: History
            source:
              path: "/instances/{instanceId}/decisions"
              query: {cell_id: "{row.cellId}", limit: 20}
            rows: items
            empty: No decisions.
            columns:
              - {path: observedAt, label: Observed, format: datetime}
              - {path: prb, label: PRB, format: percent}
              - {path: decision, label: Decision, format: badge}
              - {path: reason, label: Reason}
              - {path: outcome, label: Outcome, format: badge}
              - {path: executionId, label: Execution, format: id}
```

The routes it allows are exactly these seven: `GET /instances/{instanceId}`, `POST …/evaluate`, `POST …/reconcile`, `GET …/dashboard`,
`POST …/cells/{row.cellId}/override`, `DELETE …/cells/{row.cellId}/override`, `GET …/decisions` (the drawer's history, bound to the clicked cell by `{row.cellId}` in the query). The sample rApp declares more after these three panels (its cell-state history and its demo's
admin-only buttons, below), so the routes of the full sample are more than seven; `…/sim-producer/*` stays undeclared, so the GUI backend refuses it.

## What the four sample pages need that the declaration cannot express

The four pages share a shape: an instance block, two buttons, a cells (or relations) table, and a drawer opened from a row. With `rowDetail` all
four drawers are expressible. Read against `Mobility.tsx`, `Coverage.tsx`, `TrafficSteering.tsx` and `EnergySaving.tsx`:

| In the pages | Declared as |
|---|---|
| Per-row sparklines (PRB trend, failure-rate trend, excess, congestion score) | column format `sparkline` with `y` |
| Drawer: a larger chart of the row's trend (all four) | a `chart` block with `points` the trend field |
| Coverage drawer: three sparklines of one list (`shareTrend` with `WEAK_COVERAGE`, `OVERSHOOT`, `PILOT_POLLUTION` per point) | three `chart` blocks over the same `points: shareTrend`, `y` naming each |
| Drawer: "Latest execution (audit trail)" as JSON, "No decision yet." | a `json` block with `path: latestDecision` and `empty` |
| Drawer: the last 20 decisions of this cell or relation (`?cell_id=` for Energy Saving, Coverage and Traffic Steering, `?relation=` for Mobility) | a `table` block with a per-row `source` and `query: {cell_id: "{row.cellId}"}` (Mobility: `{row.relation}`, a column or the `rowKey`) |
| Drawer title `Cell C1`, `Relation A → B`, `Cell C1 (layer)` | `rowDetail.title` with `{row.<field>}` (the fields must be columns) |
| Operator override with a reason | `body` with `"{user}"`, or an `inputs` entry |
| The instance selector in the page header | not needed: the directory lists the rApps and the page is `/rapps/<instance>` |

What **still cannot be expressed** (each loses a little, none loses a function):

- **Text composed from several fields or computed:** `source → target`, `fromCio → toCio`, `1500 MHz → 1800 MHz` settings, the model line `id v3 (artifact 2)`,
  `CESManagementFunction.energySavingControl` derived from `actuator`. A column or item shows one field; the rApp's answer should carry the composed
  text it wants shown (an extra field), or the pieces become separate columns. The drawer title is the one place where fields are joined.
- **A map's entries** (`datasets`: stage → dataset): paths read a value or a list, not an object's keys. Shown as nothing, unless the rApp answers with a flat list.
- **Threshold colours, tooltips, inline mixed cells** (a badge followed by its reason in one cell; `Safety`, `Prediction`, `Execution` cells): each field is its own
  column, and `badge` carries the state colour.
- **Chart presentation** (`floor`, fixed height and width, a label per sparkline): the renderer decides the size and the axis; a chart `unit` is the only label.
- **A raw execution view with its own routes beyond a list** (for example opening an execution from the history table): there is no second drawer level
  (`rowDetail` does not nest). The `json` block of the row's latest decision covers the audit trail the pages show today.
- **Start, lifecycle and simulation buttons** are expressible since `role: admin`: each sample rApp declares its demo (start, the PM it plays as
  the network, train, validate, emulate on the Digital Twin, deploy) as a last `Demo / simulation (admin)` panel of admin-only actions
  (`docs/GUI_DEMO_GUIDE.md`). The governance decisions between them stay on the AI/ML page, where a person makes them; the simulator
  producer's DME callbacks stay machine routes.

## Consequences

- Onboarding and the SDK share one definition (`smo_shared/operator_ui.py`); the GUI backend carries a vendored copy of the matcher
  (its image does not install `smo_shared`; a parity test fails when the copy differs); the GUI renderer implements the same format in TypeScript and is
  tested against the example above.
- No schema migration in the first step. `GUI-8.3` added one nullable column (`operator_api_base`, revision `0030`) to `rapp_instance`, additive.
- The four sample rApps now declare their pages, their packages are rebuilt and their coded pages, sidebar entries, static permission rules and gateway routes are gone.
- A rApp author gets a bounded, reviewable page; an operator gets one place to find every rApp. The price is the list of what cannot be
  expressed above (composed text, map entries, presentation detail); anything beyond it needs a new version of this format, not a code upload.
