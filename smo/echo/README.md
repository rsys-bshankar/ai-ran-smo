# Echo, the reference ProvMnS module (`echo/`)

> Echo mounts the shared TS 28.532 ProvMnS router (`smo_shared.mns`) for two TS 28.623 classes, SubNetwork and ManagedElement, and does nothing else: every behaviour it shows is the shared library's. Its test suite is the Wave 0 exit gate of the converged rewrite (plan task S1.6).

| | |
|---|---|
| Standards basis | 3GPP TS 28.532 ProvMnS (createMOI, getMOIAttributes, modifyMOIAttributes, deleteMOI), TS 28.623 generic NRM (SubNetwork, ManagedElement), TS 28.319 MSAC, ADR 0005 scope claims |
| R1 route / port | none yet: not in R1 Termination's routing table until S1.7 / S1.8 (see 1.3); container :8000, `/ProvMnS/19.2.0/...` |
| Depends on (over R1) | nothing: Echo calls no other module |
| Called by | its tests; later every MnS consumer through R1 |
| Database tables | `echo.moi_store` (revision 0065, `create_moi_store(op, "echo")`); shared: `smo_event`, `audit_log`, `audit_head`, `idempotency_key`, `module_identity` |
| Unit tests | `tests/`, on SQLite and, with `SMO_TEST_POSTGRES_URL`, on Postgres (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done (S1.6). Off by default in compose and in the chart (1.5) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

Echo proves that a module built only from `smo_shared` is a complete ProvMnS producer: the four operations, both patch formats, scope, filter and
paging, the cross-cutting checks of S1.5 and a notification for every change. It has no class of its own, no route of its own beyond the probes, and no
lifecycle hook. A defect its suite finds is a defect of the shared library, which every later ProvMnS module (S2.7, S3.3, S4.2, S5.2, ...) inherits.

**Why SubNetwork and ManagedElement.** They are the generic classes every NRM hangs from, with no RAN- or vendor-specific attribute in the way. A
ManagedElement may sit under a SubNetwork or at the top, and not under another ManagedElement, so the registry's containment rules are tested by real
classes. A SubNetwork in a DN is what the D15 `subNetworks` axis of the scope claim reads. One class would leave containment and that axis untested. A
third class would add nothing the shared tests do not already cover.

### 1.2 Standards basis

- TS 28.532 §11.1.1 (operations) and the OpenAPI `TS28532_ProvMnS.yaml`, as realised by `smo_shared.mns` ([`../shared/README.md`](../shared/README.md)).
- TS 28.623 `TS28623_GenericNrm.yaml`: `SubNetwork-Single`, `ManagedElement-Single` (the class schemas and their containment).
- TS 28.319 MSAC (AccessRule: `dataNodeSelector`, `operations`, `action`), through `smo_shared.msac`.
- ADR 0005 (the scope claim) with the D15 axes `siteClusters` and `subNetworks`.

### 1.3 Position in the platform

```
 test client ──/ProvMnS/19.2.0/SubNetwork=A,...──▶ Echo (router of smo_shared.mns) ──▶ echo.moi_store
                                                       └─ notifications ─▶ smo_event (event bus), audit_log
```

Echo is not reached through R1 by class name yet. SME's MnS registry (S1.7) records which module serves which class. R1 Termination's routing by
registry lookup (S1.8) then sends `/ProvMnS/.../SubNetwork=...` to Echo. **The last condition of milestone M1, a ProvMnS call through R1 answered by a
module the registry names, waits for them.** Until then Echo's routes are reached directly, by its tests and by the interface gate's producer half.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `echo.moi_store` (the instances of its two classes) | The router, the class registry, the checks → `smo_shared.mns` |
| Its two settings, `ECHO_PLACEMENT` and `ECHO_MSAC_RULES` | TS 28.319 AccessRules as ProvMnS classes → S1.14 |
| | Routing by class through R1 → S1.7, S1.8 |

### 1.5 Design decisions

- **Nothing of its own.** `app/main.py` names the classes and the system DN, builds the checks from two settings and mounts `provmns_router`. A
  behaviour that would need Echo code belongs in `smo_shared.mns`.
- **Off by default.** Compose profile `echo` (`docker compose --profile echo up echo`) and `modules.echo.enabled: false` in the chart. Echo is a
  reference and test module with no operator function. Its write API would let anyone the gateway admits create objects nothing reads, and once S1.8
  routes by class, a SubNetwork served by Echo would compete with the real producer of that class. It is switched on for tests and for demonstrating
  the router.
- **Settings read per request.** Placement and MSAC rules are environment variables read on every operation, so a test or an operator changes them
  without a restart. Both fail closed: placement that cannot be read places nothing (only unscoped and SubNetwork-scoped callers reach anything), and
  MSAC rules that cannot be read, or any one rule that is not a valid AccessRule, refuse every caller.
- **The image carries the specifications.** The class registry reads `specs/5G_APIs` at run time. The Dockerfile copies it from the build context
  `specs` into `/srv/specs/5G_APIs`. Without it Echo refuses to start (the registry finds no class files). The copy adds 13.2 MB to every module's
  image (460.7 MB → 473.9 MB, +2.9%; the layer is 12.2 MB in 544 files). Every image gets it because there is one Dockerfile, and every module mounts the
  router from Stage 1 on.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | `CLASSES`, `SYSTEM_DN`, `placement(dn)` (ECHO_PLACEMENT through `PrefixPlacement`), `msac_rules(caller)` and `_rule(item)` (ECHO_MSAC_RULES), `CHECKS`, `mns`, the FastAPI app with logging, metrics, gateway security, correlation id, health and the router |
| `app/models.py` | `MOI_STORE`: the shared `ManagedObject` table placed on the shared `Base`, so `create_all` makes it in the SQLite tests and the models check knows it |
| `../migrations/versions/0065_echo_schema.py` | schema `echo` and its `moi_store` (`create_moi_store(op, "echo")`); downgrade drops both |

### 2.2 Data model

`echo.moi_store` is `smo_shared.mns.store`'s table (DN, class, parent, attributes, version, ...), unchanged. Role `smo_echo` (`migrations/db_roles.json`)
owns schema `echo` and reaches the shared tables it writes. Its `search_path` is `echo, public` (`scripts/db_roles.py`). `moi_store` is listed under
Echo's role only in `migrations/table_owners.json`.

### 2.3 State machines

None. An instance exists or it does not, with a `moiVersion` that each change raises (the `ETag`).

### 2.4 API

The one ProvMnS route of the router, `/ProvMnS/{version}/{ldn}`: `PUT` createMOI, `GET` getMOIAttributes, `PATCH` modifyMOIAttributes, `DELETE`
deleteMOI, for SubNetwork and ManagedElement. Contract: [`../../specs/smo-schema-bundle/openapi/modules/echo.yaml`](../../specs/smo-schema-bundle/openapi/modules/echo.yaml)
(the TS 28.623 class schemas by `$ref`; the interface gate and the drift check count it as a bundle operation, with zero drift). Probes: `/live`,
`/ready`, `/health`, `/version`, `/metrics`.

### 2.5 Interactions

Every check of S1.5 runs in the router, in this order (`smo_shared/mns/checks.py`):

1. caller and scope: the ADR 0005 claim from the gateway's headers against the target's placement (`ECHO_PLACEMENT`) and its SubNetworks (from the DN);
2. MSAC: the caller's AccessRules from `ECHO_MSAC_RULES` (a caller not listed is not managed);
3. idempotency: a createMOI with `Idempotency-Key` replays its first answer;
4. audit: each committed write adds a row to the hash chain, in the write's transaction;
5. traceparent: the notification's event row and envelope keep the request's trace;
6. metric: `smo_provmns_http_duration_seconds{module="echo", class, operation, outcome}`.

modifyMOIAttributes has no `Idempotency-Key`. No route in the platform applies it to a modify (only to POST creations), and a PATCH is protected by
`If-Match` instead.

### 2.6 Configuration

| Variable | Meaning | Default |
|---|---|---|
| `ECHO_PLACEMENT` | JSON: DN prefix → `{region, tenant, siteCluster}` | empty: nothing placed |
| `ECHO_MSAC_RULES` | JSON: caller (invoker id or console user) → list of `{dataNodeSelector, operations, action}` | empty: no caller managed |

The common variables (database, enrollment secret, logging, tracing) are in [`../docs/CONFIGURATION.md`](../docs/CONFIGURATION.md).

### 2.7 Error codes

The router's TS 28.623 bodies ([`../shared/README.md`](../shared/README.md#27-error-codes)), with S1.5's: 403 `SCOPE_DENIED`, 403 `MSAC_ACCESS_DENIED`. A
read outside the caller's reach is 404 `MOI_NOT_FOUND`.

### 2.8 Limits and open items

- Not routed through R1 until S1.7 and S1.8 (1.3).
- Placement is a setting, not data. Once S1.14 keeps TS 28.319 AccessRules as ProvMnS classes, the authorizer reads them instead (`../shared/README.md`, "How S1.14 plugs in").

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/echo && PYTHONPATH=.:../shared python -m pytest tests/ -q
# on Postgres too (migrated to head first):
SMO_TEST_POSTGRES_URL=postgresql+psycopg://smo:...@localhost:5432/smo PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Every test in `test_echo.py` runs on SQLite and on Postgres (the `echo` fixture). Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| Test file | Covers |
|---|---|
| [`tests/test_echo.py`](tests/test_echo.py) | the Wave 0 exit gate: every operation on both classes; both merge-patch and both JSON-patch media types; containment and an unserved class; each scope type, a filter and paging; a notification for every change and none for a read; one test per S1.5 check (scope claim with the D15 axes, MSAC, Idempotency-Key, audit chain, traceparent, metric); the probes and the one route |
| [`tests/test_settings.py`](tests/test_settings.py) | `ECHO_PLACEMENT` and `ECHO_MSAC_RULES`: placement by prefix, rules per caller, and each way a setting fails closed; the S1.3 dictionary reads the same specifications as Echo's registry |

Mutation testing: `scripts/mutation_pilot.sh` mutates `app/` (configuration in `pyproject.toml`); every mutant is killed.

### 3.3 What is not covered here

- The router itself in depth: `shared/tests/test_mns_*.py`.
- The image (the specs layer, the role's `search_path`): `docker compose --profile echo up echo` by hand. No CI job starts Echo's container.
- R1 routing: S1.8.

## 4. References

- Lifecycle: [`docs/lifecycle.md`](docs/lifecycle.md)
- OpenAPI: [`../docs/openapi/echo.json`](../docs/openapi/echo.json); contract: [`echo.yaml`](../../specs/smo-schema-bundle/openapi/modules/echo.yaml)
- Shared library: [`../shared/README.md`](../shared/README.md); ADRs [0005](../docs/adr/0005-tenant-region-authorization.md), [0008](../docs/adr/0008-converged-architecture.md), [0011](../docs/adr/0011-standard-interfaces-only.md)
- Plan: [`../docs/plan/CONVERGED_REWRITE_PLAN.md`](../docs/plan/CONVERGED_REWRITE_PLAN.md) (S1.3, S1.5, S1.6, S1.7, S1.8, S1.14)
