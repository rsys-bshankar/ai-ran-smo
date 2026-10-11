# Data & Exposure
Route: /data    Owner: SMO GUI    Design: `docs/redesign/designs/Data.dc.html`, SCALE.md "Data & Exposure"

Tabs (URL hash): `#dme` Flow & jobs (default; the pre-redesign DME tab id), `#offers` Producers & offers, `#sme` SME. Links to `/data#sme` keep
working. Each tab mounts only its own sections.

Every word on the page comes from the catalog (`src/i18n/en/data.ts`, `de/data.ts`; GUI-6.4); its literal count is 0.

## Sections
| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| data.flow | sections/DataFlow.tsx | producers → data types → consumers, band width = jobs, ≤ 8 rows a column with "+N other" (`data/flow.ts`), job total and late badges, cut notes | `/dme/dme-types`, `/dme/data-jobs?limit=500`, `/dme/offers?limit=500`, `/dme/data-jobs?late=true&limit=1` (total) | 15 s | 4 calls |
| data.jobs | sections/DataJobs.tsx | create a data job (methods the type's offers committed), server table with `?dme_type_id=`, `?consumer_id=` and "late only" (`?late=true`) filters, last delivery with a LATE badge, terminate, JSON view | `/dme/data-jobs`, `/dme/offers?dme_type_id=` (after a type is chosen) | 15 s | 1 call/page |
| data.producers | sections/Producers.tsx | registered producers, deregister | `/dme/production-capabilities` (unpaged) | 15 s | 1 call |
| data.types | sections/Producers.tsx | data types, status, delete, admin "register a producer data type" | `/dme/dme-types` (unpaged) | 15 s | 1 call (shared) |
| data.offers | sections/Offers.tsx | offers with a type filter, notify data ready, terminate, admin create | `/dme/offers` | 15 s | 1 call/page |
| data.type-subscriptions | sections/Offers.tsx | type subscriptions, subscribe / unsubscribe | `/dme/type-subscriptions` | 15 s | 1 call/page |
| data.providers | sections/ExposedServices.tsx | API providers (server table), register / deregister; a row picks the provider | `/sme/provider-registrations` | 15 s | 1 call/page |
| data.services | sections/ExposedServices.tsx | the picked provider's published service APIs, unpublish, admin publish | `/sme/published-apis/v1/{apfId}/service-apis` (unpaged) | 15 s | 1 call |
| data.invokers | sections/Invokers.tsx | onboarded invokers (server table), trust / remove trust, offboard, onboard (one-time secret), trusted count | `/sme/invoker-registrations`, `/sme/trusted-invokers?limit=1` | 15 s | 2 calls |
| data.discovery | sections/Discovery.tsx | discovery as one invoker, with the route's `?api_name=` search | `/sme/service-apis/v1/allServiceAPIs`; the invoker list (`?limit=500`) is read when the picker is first used | 15 s | 0 until used |
| data.capif-subscriptions | sections/Discovery.tsx | CAPIF event subscriptions of one subscriber, with filters | `/sme/capif-events/v1/{subscriber}/subscriptions` | 15 s | 1 call/page |

First-load calls per tab: Flow & jobs 4 (types, flow jobs, flow offers, jobs page), Producers & offers 4, SME 5.

## Known limits
- **LATE** is judged only for a job that declares an expected delivery interval (two intervals without a delivery); a job without one shows its
  last delivery and never LATE. DME records a delivery when a producer posts records to it; a producer delivering straight to the consumer is
  not seen.
- The flow is drawn from the first 500 jobs and 500 offers; past that the box says so and the jobs table pages through all of them.
- DME does not record which producer serves a job, so a type served by several producers counts in each producer's band.
- Consumers are grouped by `consumerId`; there is no consumer category in DME.
- `/dme/dme-types`, `/dme/production-capabilities` and `/sme/published-apis/…/service-apis` are not paged by the modules: they are registries
  read whole. The published-services route has no search; search across services is the discovery box (`?api_name=`), as one invoker.
- There is no DME or SME count in the BFF summary, so the tabs show no counts.

## Troubleshooting
- Flow box empty but jobs exist: `/dme/dme-types` failed or returned nothing; check DME.
- "Drawn from the first 500 of N data jobs": expected past 500 jobs; the bands are a sample, the table is complete.
- Discovery lists nothing: the invoker is not in any service's `allowedConsumers`, or the API name filter matches no service.

## Upgrade notes
- Redesign: the page moved from `pages/Data.tsx` to this folder; DME split into "flow & jobs" and "producers & offers"; lists are server-paged.
