# RAN topology

Route: `/topology` (`?me=<element>` focuses one element, `?problem=external|ambiguous` picks the problem table's filter)    Design: handoff `Topology.dc.html` (BRIEF §4e feature 1)

Every word on the page comes from the catalog (`src/i18n/en/topology.ts`, `de/topology.ts`, GUI-6.4); its literal count is 0.

The Dashboard's "Open topology" link lands here. Every element in a graph node, a problem row or the summary links to its Element detail page (`/elements/<me>`).

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| topology.export | sections/ExportTeiv.tsx | "Export (TEIV JSON)" download in the header | `GET /ran-nf-oam/topology[?managed_element_ref]` | on click | 0 on load |
| topology.tiles | sections/RelationTiles.tsx | cells with guards, elements, relations, not reciprocal (between managed cells), external, ambiguous (problem tiles filter the table) | `/topology/links/counts`, `/cell-guards?limit=1` total, `/managed-entities?limit=1` total | 60 s | 3 calls |
| topology.graph | sections/NeighbourGraph.tsx | the focused element, its cells and first-ring neighbours (≤ 200 nodes, dashed = not reciprocal); picker with server-search suggestions | `/topology/links?managed_element_ref`, `/managed-entities/{me}`, `/managed-entities?search=&limit=20` (from 2 characters, 200 ms after typing) | 60 s | 2 when focused + 1 per pause in typing |
| topology.check | sections/RelationCheck.tsx | how DN A stands to DN B in the containment tree | `/topology/relation?a&b` | on Check | 0 on load |
| topology.element | sections/ElementSummary.tsx | vendor, type, region/tenant, cells, sector groups, incident zones, non-NORMAL guards, links | `/managed-entities/{me}` (shared with the graph) | 60 s | 0 extra |
| topology.containment | sections/ContainmentGraph.tsx | the managed-object containment tree, folded at the element roots (open to the leaves for a focused element), each node coloured by its worst open alarm and a folded node by the worst below it; a node's name opens its element's Managed objects tab (GUI-3) | `/topology/graph?max_nodes=500[&managed_element_ref]` (scoped by the top bar) | 15 s | 1 call |
| topology.problems | sections/ProblemRelations.tsx | relations that need attention (not reciprocal between elements or within one / external / ambiguous), server-paged, everywhere or around the focused element, with the fix and a link to the cell guards | `/topology/links?reciprocal=false&link_type=INTER_ELEMENT\|INTRA_ELEMENT` or `?link_type=EXTERNAL\|AMBIGUOUS`, `&managed_element_ref&limit&offset` | 60 s | 1 call/page |

Pure rules (fix hint, graph layout and the 200-node cap; counting a list for the graph) are in `data/graph.ts`; the containment tree's (building
it, the worst alarm of a subtree, the visible rows and their 400-row cap) in `data/containment.ts`.

## Known limits

- RAN NF OAM computes the relations from every cell guard on each call (the counts and each page alike); paging bounds what the browser gets,
  not the server's work.
- "Not reciprocal" between elements and within one element are two server filters (`link_type` cannot be OR-ed), so the table shows one at a
  time ("Between"); the tile counts both.
- The focus picker suggests up to 20 elements whose ref or name contains the text; it still takes any exact ref typed.
- The mockup's "Cells in scope … DUs · metro-a" is shown as "cells with guards" (only a cell with a guard is known to the registry) and the
  total of managed elements; the region comes from the top bar's scope picker (below), not from a picker on this page.
- Alarm badges on the neighbour graph's cells (the mockup's "cell-7 LOS") are not shown there; the containment tree below carries the alarm
  overlay (each cell's open alarms, placed by its `managedFunctionRef`).
- The containment tree asks for the first 500 objects in DN order; a larger network is cut (the box says so): focus one element, or narrow the
  scope. An alarm whose node and element root are both past the cut is not on the page.
- The graph shows only relations declared in cell guards (`neighbourRefs`); TEIV RAN-domain relations (Xn, F1) are not modelled by the backend.
- **Scope** (GUI-9.3): the link list and its counts (either end in the scope), the cell guards and the element list follow the top bar's scope; the TEIV export (`/topology`) and a relation (`/topology/relation`) are network-wide.

## Troubleshooting

- Graph says "declares no neighbour relations": the element's cell guards have no `neighbourRefs` (Element detail → Cell guards).
- Relation check answers "Not in the containment tree": a DN is not a node of `/managed-objects` (compare exact text, case-sensitive);
  refresh the element's tree or register its function.
- Tiles show "—": `/topology/links` failed; the box shows the error with Retry. Check RAN NF OAM's logs.

## Upgrade notes

- v1 (GUI redesign): new page.
