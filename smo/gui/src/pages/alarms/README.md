# Alarms

Route: `/alarms` (`?me=<managed element>` starts the table filtered on it; the global search links there)    Design: `Alarms.dc.html`, SCALE.md "Alarms · at scale"

Tabs (URL hash): `#ran` RAN NF alarms · `#ocloud` O-Cloud alarms · `#fm` FM subscriptions. Only the visible tab's queries run.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| alarms.tiles | sections/SeverityTiles.tsx | count per severity (toggles the table filter), open total + unacknowledged, mean time to acknowledge (24 h), alarms raised per hour sparkline | `/api/summary/alarms`; `/ran-nf-oam/alarms/counts?group_by=hour` | 15 s (pushed when live) / 60 s | 1 call + 1 (shared with the Dashboard) |
| alarms.table | sections/AlarmTable.tsx | RAN alarms, keyset-paged in console order (most severe, newest); filters severity, managed element, managed function, ack state, probable cause, show cleared (else `open_only=true`); group by probable cause / element / severity / ack state / region, a group opens a filtered table; "N new alarms — show" bar; Export… (operator, GUI-2.5): every alarm the filters and the scope select, as an export job listed on Exports | `/ran-nf-oam/alarms?after&limit&severity&managed_element_ref&managed_function_ref&ack_state&probable_cause&open_only`; `/ran-nf-oam/alarms/counts?group_by=…` | 5 s, 60 s while live (an alarm count change refetches at once) | 1 call per page |
| alarms.detail | sections/AlarmDetail.tsx | selected alarm: Ack/Unack/Clear, TS 28.532 fields, ack time, lifecycle timeline with ack and clear times | — (the table's row) | with the table | 0 |
| alarms.history | sections/AlarmNotes.tsx | GUI-2.4: inside the detail, every change of the alarm the server recorded (raised, acknowledged or withdrawn, re-graded, cleared), oldest first, with who | `/ran-nf-oam/alarms/{id}/history?limit=100` | 60 s | 1 call |
| alarms.comments | sections/AlarmNotes.tsx | GUI-2.3: inside the detail, the operators' comments, oldest first, and "Add a comment" (operator; the BFF writes the signed-in user as the author) | `/ran-nf-oam/alarms/{id}/comments?limit=100`, `POST` the same path | 60 s | 1 call |
| alarms.rootcause | sections/RootCauseHint.tsx | the server's correlation: alarms of the same element within ±60 s (rule named) | `/ran-nf-oam/alarms/{id}/correlated?window_seconds=60` | 15 s | 1 call per selected alarm |
| alarms.inject | sections/InjectAlarm.tsx | admin: inject a test alarm | `POST /ran-nf-oam/alarms/ingest` | — | 0 |
| alarms.ocloud | sections/OCloudAlarms.tsx | FOCOM alarms, server-paged, severity / resource filters | `/focom/alarms?severity&resource_ref` | 5 s | 1 call |
| alarms.fm | sections/FmSubscriptions.tsx | new FM subscription and Unsubscribe (operator: BFF rules `POST /ran-nf-oam/fm-subscriptions`, `DELETE …/{id}`, GUI-10.1), the list | `/ran-nf-oam/fm-subscriptions`, `/ran-nf-oam/o1-adaptor-endpoints` | 15 s | 2 calls |

`sections/AlarmActions.tsx` holds the Ack / Unack / Clear buttons the table and the detail share.

First load (RAN tab): summary + one page + the hourly counts = 3 calls (SCALE.md §4 budget is 2; the hourly buckets are shared with the Dashboard's cache entry).

## Known limits

- **Root-cause hint** is the server's stated rule (same managed element, raised within ±60 s), not a topology-aware root-cause analysis.
- **Group by** shows the 50 largest groups; a group with no key ("(none)") cannot be listed by that key. Bulk ack on a group has no route.
- The **"N new" bar** compares the summary's `alarms.total` with its value when the operator last looked; alarms raised outside the current
  filters count too.
- **Mean time to acknowledge** covers alarms acknowledged in the last 24 h; alarms acknowledged before the backend kept `ackTime` have none.
- The managed element filter is a text box (exact match on `managed_element_ref`), not a list built from every alarm. The sparkline's hours are UTC.
- "Assign…" from the mockup has no backend.
- **Scope** (GUI-9.3): the RAN alarm list, its tiles, hourly counts and group counts follow the top bar's scope (`region`, `site_cluster`); the Region filter of the table overrides it. O-Cloud (FOCOM) alarms know no region and stay network-wide (the tiles say so).

## Troubleshooting

- Tiles show "—": the summary's module did not answer (`partial` is listed under the tiles); check `/api/summary/alarms`.
- Table empty with a `?me=` link: the element name must match `managedElementRef` exactly. Cleared alarms are hidden unless "show cleared" is on.
- Ack / Clear missing: the role lacks `PATCH /ran-nf-oam/alarms/{id}/ack|clear` (operator and up).
