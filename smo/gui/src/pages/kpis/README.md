# KPIs & Assurance

Route: `/kpis`    Design: handoff `Kpis.dc.html` (BRIEF §4 KPIs & Assurance, §4e feature 9; SCALE.md "KPIs & Assurance")

Network performance, the monitors that watch it, and what was done when a threshold broke. Tabs (URL hash): `overview` (default, new),
`pm`, `definitions`, `rapp`, `analytics`, `assurance`, `ocloud`, `mlmf`. The pre-redesign ids are unchanged; `mlmf` mounts
`pages/aiml` `Mlmf`.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| kpis.tiles | sections/KpiTiles.tsx | 5 standard KPIs over 1 h / 24 h / 7 d, in the top bar's scope | `/ran-nf-oam/kpis/{name}?from_time&group_by=all&region&site_cluster` ×5 | 60 s | 5 calls |
| kpis.chart | sections/KpiChart.tsx | up to 8 KPIs over time (GUI-4.1), narrowed to a region and site cluster that start at the top bar's scope (GUI-4.2); the user's saved layouts: load, save as, delete (GUI-4.3) | `/ran-nf-oam/kpis/{name}/series?from_time&step_seconds&region&site_cluster` per KPI, `/ran-nf-oam/managed-entities/scopes`, `/ran-nf-oam/kpi-definitions?limit=200`; BFF `/api/me/kpi-layouts` | 60 s (series) | 3 calls + 1 per KPI |
| kpis.worst | sections/WorstElements.tsx | 10 elements with the lowest DL UE throughput, in the top bar's scope | `/ran-nf-oam/kpis/dl_ue_throughput?group_by=element` | 60 s | 1 call |
| kpis.monitors | sections/Monitors.tsx | assurance monitors, server-paged; click → panel | `/sa-smos/monitors` | 15 s | 1 call/page |
| kpis.monitor | sections/MonitorPanel.tsx | evaluate, remediate, escalate; actions taken | `/sa-smos/remedial-actions?monitor_id=&limit=1` | 15 s | 1 call |
| kpis.escalations | sections/Escalations.tsx | newest 5 escalated actions + true count | `/sa-smos/remedial-actions?outcome=ESCALATED&limit=5` | 15 s | 1 call |
| kpis.pm | sections/PmSubscriptions.tsx | PM subscriptions, server-paged; new subscription; Unsubscribe (operator: BFF rule `DELETE /ran-nf-oam/pm-subscriptions/{id}`, GUI-10.1) | `/ran-nf-oam/pm-subscriptions`, `/o1-adaptor-endpoints` | 15 s | 1–2 calls |
| kpis.definitions / schedules | sections/KpiDefinitions.tsx (+ KpiDialogs.tsx) | KPI catalogue and DME publishing schedules | `/ran-nf-oam/kpi-definitions?limit=200`, `/kpi-schedules` | 15 s | 2 calls |
| kpis.rapp | sections/RappPerformance.tsx | one instance's self-reported metrics | `/rapp-mgmt/instances`, `/…/{id}/performance?limit=100` | 15 s | 2 calls |
| kpis.mdaRequest | sections/MdaRequestForm.tsx | request an analysis (operator and up; a viewer sees a note) | `/mdaf/mda-functions` (form only), `POST /mdaf/mda-requests` | — | 0–1 call |
| kpis.mdaFunctions / mdaRequests / mdaReports | sections/MdaLists.tsx | TS 28.104 functions, requests (Cancel: `DELETE /mdaf/mda-requests/{id}`), reports (kind filter, file download) | `/mdaf/mda-functions`, `/mda-requests`, `/mda-reports?report_kind=` | 15 s | 3 calls |
| kpis.analyticsReports / producers / analyticsSubs | sections/AnalyticsLegacy.tsx | legacy MDAF reports, producers (Deregister, admin: BFF rule `DELETE /ran-analytics/producers/{id}`, call flow 08a, GUI-11.1), subscriptions | `/mdaf/reports`, `/ran-analytics/producers` (`DELETE …/{id}?analytics_type`), `/mdaf/subscriptions` | 15 s | 3–4 calls |
| kpis.producerTools | sections/ProducerTools.tsx | admin: register a producer, publish a report | `/dme/dme-types` | — | 1 call |
| kpis.newMonitor | sections/RegisterMonitor.tsx | register a monitor (operator+) | `/so-smos/orders`, `/aimgf/coordination-groups`, `/rapp-mgmt/instances` | 15 s | 3 calls |
| kpis.actions | sections/RemedialActions.tsx | remedial actions, `outcome` filter | `/sa-smos/remedial-actions`, `/sa-smos/monitors?limit=500` | 15–60 s | 2 calls |
| kpis.ocloud | sections/OCloudPerformance.tsx | FOCOM performance, `resource_ref` filter | `/focom/performance` | 15 s | 1 call/page |

First load of the Overview: 12 calls (5 tiles + the chart's series, scope list, definitions and layouts + worst list + monitors +
escalations), over the 6-call target: RAN NF OAM has no batch KPI route, so each tile and each charted KPI is its own computation. The KPI
reads poll every 60 s. A saved layout of eight KPIs adds seven series calls.

Every word on the page comes from the catalog (`src/i18n/en/kpis.ts`, `de/kpis.ts`; GUI-6.4); its literal count is 0.

## Known limits

- **KPI sparklines and the throughput band chart** (p10–p90 over time): the tiles carry no sparkline (that would be five more series calls;
  the chart box below them draws any KPI over time), and the band chart is replaced by the worst-10 list (with a note): the series is one value
  per step over the place, not a distribution over its cells.
- **Per-cell heatmap / worst cells**: the worst list is per managed element (`group_by=element`); a per-cell ranking would be the same call with
  `group_by=cell` and is left out to keep the Overview's call count down.
- **Assurance monitors "breaching first"**: SA SMOS keeps no breach state on a monitor and the list has no such filter; the table is in server
  order and says so. "Now" values per monitor are not served (a monitor is evaluated on demand in its panel).
- **Request an analysis** (feature 9): the report kind (ANALYTICS / PREDICTION / DRIFT) is not part of a request (MDAF types each report), so it
  is a filter on the reports table instead.
- **Scope picker**: the tiles, the worst list and the chart follow the top bar's scope (`region`, `site_cluster` on the KPI routes, GUI-4.2);
  the chart's own region select narrows the charts alone. A step with no data is left out of the line (the caption counts them), not drawn
  as a gap.

## Troubleshooting

- A tile says "not defined (Definitions tab)": that standard KPI is not defined; "Add the standard set" in Definitions (admin).
- Tiles say "no data in window": no PM file with the KPI's counters arrived in the range; check PM subscriptions and the DME jobs.
- "The server capped the computation": RAN NF OAM read its file or group limit (`truncated`); narrow the range.
- A chart says "not defined (Definitions tab)": a saved layout names a KPI that was deleted since; take it off (×) and save the layout again.
- "Save layout" stays disabled: the name is 1 to 60 letters, digits, spaces, `.`, `_` or `-`, starting with a letter or digit. A 21st layout is
  refused (a toast says so): delete one first.
