# Element detail

Route: `/elements/:me` (`me` = the URL-encoded managed element ref; tabs in the hash: `#overview`, `#history`, `#mo`, `#guards`;
`?mo=<dn>`, `?from=<snapshot>`, `?to=<snapshot>`, `?cell=<cell id or +>`)    Design: handoff `Element.dc.html` (BRIEF §4e feature 4)

Linked from the RAN topology page (graph nodes, problem rows, summary), the Dashboard and the global search (`/elements/<me>`).
`data/types.ts` holds the RAN inventory types the Topology and Software pages also use; `data/url.ts` the `?name=` state hook all four new
RAN pages share.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| element.header | sections/Header.tsx | ref, root DN, vendor and O1 protocol, type, region / tenant, site cluster (admin edits it in place), critical alarm badge; Neighbours, Upgrade software, New config job | `/managed-entities/{me}`, `/alarms?managed_element_ref&severity=critical&limit=1`; PUT `/managed-entities/{me}/site-cluster` | 60 s / 10 s | 2 calls |
| element.overview | sections/Overview.tsx | critical / all alarms, cells and guards, config changes, neighbour relations (one-way count); O1 endpoint card (adaptor, services, conformance, host key, onboarding) | `/alarms?…&limit=1` total, `/config-history?limit=1` total, `/topology/links?managed_element_ref`, `/o1-adaptor-endpoints/{id}/host-keys`, `/element-onboarding/{me}` | 10–60 s | 5 calls |
| element.history | sections/ConfigHistory.tsx | every dispatched write, newest first, server-paged, function filter; pick "from" / "to"; "Undo job…" (rollback with preview) | `/managed-entities/{me}/config-history?limit&offset&managed_function_ref`; POST `/config-jobs/{id}/rollback` | 15 s | 1 call/page |
| element.diff | sections/SnapshotDiff.tsx | -/+ lines between the two picked snapshots (`kit/Diff`) | `/managed-entities/{me}/config-history/diff?from_snapshot&to_snapshot` | on pick | 1 call |
| element.mo | sections/MoTree.tsx | containment tree from the root DN; children load on expand (50, then "more"); "Expand 3 levels" (subtree); Refresh from element | `/managed-objects/{dn}`, `/children?limit=`, `/subtree?depth=3`; POST `/managed-entities/{me}/managed-objects/refresh` | on expand | 1 call + 1 per opened node |
| element.attributes | sections/MoAttributes.tsx | the selected object (DN, class, id, parent, source); live attribute values on request | `/managed-objects/{dn}`; `/managed-entities/{me}/config?managed_function_ref=` (southbound read) | on request | 1 call |
| element.guards | sections/CellGuards.tsx | each cell's guard: class, sector group, incident zone, neighbours; Edit / Add (admin) | `/cell-guards?managed_element_ref=` | 15 s | 1 call/page |
| element.guard-editor | sections/GuardEditor.tsx | class NORMAL / COVERAGE_CRITICAL / EMERGENCY, sector group, incident zone, neighbours; Save, Remove | `PUT` / `DELETE /managed-entities/{me}/cells/{cell}/guards` | — | 0 (reads the element, shared) |

Every word on the page comes from the catalog (`src/i18n/en/element.ts`, `de/element.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **Refresh from element** walks the element's server synchronously; a large element can take a while and the button waits for it.
- **Config history is offset-paged**, not keyset: the route has no `after=` cursor (SCALE.md asks for one).
- **Roll back to a snapshot** is "undo the job that wrote it": the backend's rollback route undoes a whole job from its snapshots
  (`POST /config-jobs/{id}/rollback`); there is no "restore this snapshot" route.
- The mockup's cell table (admin / operational state, PCI, PRB load per cell) and "heartbeat late" are not served per element; cells show their
  guards only. Writable-or-not per attribute is not served either.
- Live attribute values come from the element itself (`/config`); the tree holds names only. A read can fail when the endpoint is down (503).
- Host key in the overview: the route answers 422 for an endpoint that is not ssh; the card then says so.
- **Scope** (GUI-9.3): the element's own reads are by element; its alarm list and cell guards also carry the top bar's scope, so an element outside the scope shows them empty (clear the scope with "All network").

## Troubleshooting

- Header says no element is registered: the ref in the URL does not match `/managed-entities` exactly (case-sensitive).
- Tree says "not in the containment tree yet": the element was registered before PR-SB-6, or its tree was never built; register its function.
- Diff answers 422: the two snapshots are of different managed functions; filter the history by function first.
- Edit is missing on Cell guards: setting a guard is an admin's call.

## Upgrade notes

- v1 (GUI redesign): new page.
