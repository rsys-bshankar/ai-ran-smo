### MODULE-MANIFESTS — Each module declares itself in `<module>/module.yaml`; the shared files that listed modules are generated from, or read from, the manifests

**Why.** After `PARALLEL-SAFE`, the four pull requests that followed were all `[cross-lane]`: adding or wiring a module still meant editing the same
shared lists, namely the module services of `docker-compose.yml`, the chart's `modules:`, `migrations/db_roles.json` and `table_owners.json`, R1
Termination's `ROUTES`, `scripts/generate_openapi_specs.py`'s list, the CI unit loop, `coverage_floors.json`, the mutation pilot and its workflow's
paths, and the unit loop in `CLAUDE.md`. The fix is the one `PARALLEL-SAFE` used: each module says what it is in one file of its own folder, and the
shared code reads those files.

**The manifest** (`<module>/module.yaml`, JSON Schema `scripts/module.schema.json`, every key required so that "no part in this" is written down):
- `name` (the directory's name) and `lane`;
- `r1`: R1 Termination's static routes to the module (prefix, the variable that overrides the address, the default address). ProvMnS is not
  listed: it is routed by the MnS registry (`r1-termination/app/mns_routing.py`, `PR-CONV-S1.8`);
- `database`: the role `scripts/db_roles.py` makes (`schema`, `shared`, and `read` for R1 Termination's read of the kill switch), and the tables the
  module owns;
- `openapi`: whether `generate_openapi_specs.py` writes its spec;
- `tests`: the unit suite (what coverage measures, the PYTHONPATH) and its coverage floor;
- `mutation`: the pilot's PYTHONPATH (`$SMO` is the absolute `smo/` path), the targets, and the paths that run `smo-mutation.yml` on a pull request;
- `code_docs`, `config_reference`: whether the module has a file in `scripts/code_docs_baseline/` and in `docs/configuration/`;
- `compose`: the module's text in each top-level section of `docker-compose.yml` (`services`, `secrets`, `volumes`, `networks`), verbatim, comments
  included, using the file's anchors (`*hardening`, `*ready_probe`, `*db_env`, ...);
- `helm`: the module's entries under the chart's `modules:`, verbatim. It is not "the same as compose": the chart's entries say different things
  (an image name, `databaseRole`, `kind: worker`, sizing), and `tests_integration/test_helm_chart.py` still checks that the two agree.

**`scripts/modules.py`** loads every manifest, validates it against the schema, and checks it against its module and the others:
- a folder with code in `app/` or `tests/` and no manifest fails;
- a module with a package and tests but no `tests` fails (its suite would silently stop running);
- an R1 prefix or variable, a table, a database schema, a compose key or a chart entry claimed by two manifests fails;
- a compose service or chart entry not named `<name>` or `<name>-<role>` fails;
- `mutation.targets` must equal the module's `[tool.mutmut] only_mutate`;
- `code_docs` and `config_reference` must match the files that exist.
Then it regenerates the shared files and, with `--check` (CI, step "Module manifests valid and generated files current" of `unit-and-integration`),
fails when one differs. `list unit-suites | openapi | mutation | database` prints what the scripts read.

**What each shared list became.**
- **`docker-compose.yml`: generated into the committed file, not `include`d per module.** Twenty-three scripts and tests read
  `docker-compose.yml` directly (compose checks, the helm-agreement test, the configuration reference, the mTLS and probe checks). The module
  services also use the file's anchors, and compose `include` does not share anchors between files. So the file stays one file. Between
  `# >>> modules` and `# <<< modules` in `services:`, `secrets:`, `networks:` and `volumes:`, each module's text sits under a header line naming its
  manifest, in path order. `migrate` and `pgbouncer` mount every module's password secret: that list is a generated region too, one line per
  module. The infrastructure services, the anchors and the comments outside the regions are edited by hand as before.
- **The chart's `values.yaml`:** a generated region under `modules:`.
- **`migrations/db_roles.json` and `table_owners.json`:** generated, keys in path order. The `_` keys (`_comment`, `_retired`) are kept from the file.
- **R1 Termination's `ROUTES`:** generated into the committed `r1-termination/app/routes.json`, which `app/main.py` reads at import. It is not
  built by walking the other modules' folders at start-up, for two reasons: the gateway's image holds only its own module (`Dockerfile`:
  `COPY ${MODULE}/app`), and a change to the routing table should be reviewed as a diff of one file. `tests_integration/mesh.py` reads the same file.
  `scripts/config_reference.py` learned `# config-ref-table: routes.json`, so the address variables stay documented under R1 Termination.
- **Read at run time:**
  - `generate_openapi_specs.py`'s modules, `check_migration_matches_models.py`'s modules (those with a database role: the same 19);
  - the CI unit loop (one loop over `list unit-suites`, the same 23 suites with the same arguments, replacing four steps);
  - the coverage floors (`coverage_floor.py` reads `tests.floor`; `--write` rewrites that line of each manifest);
  - the mutation pilot (a loop over `list mutation`, the shared library first);
  - `CLAUDE.md`'s unit loop.
- **`smo-mutation.yml`:** its `pull_request` paths have a generated region from the modules' `mutation.watch`.

**Proof that nothing changed.** Against `main` (94e2adb):
- `docker compose config --format json` is equal, both by default and with every profile: 32 and 52 services. The only difference is the order of
  `migrate`'s and `pgbouncer`'s secrets, which no code depends on (`db_roles.py` makes a role for each password file it finds).
- Every line of the old `docker-compose.yml`, `values.yaml` and `smo-mutation.yml` is in the new file the same number of times; the new files add
  only markers, headers and blank lines, apart from the one-line secret lists turned into a list of lines.
- The parsed values are equal, and so are the JSON values of `db_roles.json` and `table_owners.json`.
- `ROUTES` and the mesh's table are equal to `routes.json`.
- The configuration reference regenerates with no diff.
`tests_integration/test_module_manifests.py` holds the gates:
- an edit by hand inside a generated region is stale;
- a manifest outside the schema, a module with no manifest, a double claim, a dropped unit suite, wrong mutation targets and wrong `code_docs` /
  `config_reference` flags each fail;
- the coverage gate still fails in its four cases (below the floor, more than 3 points above it, measured with no floor, a floor with no
  measurement);
- the lists read at run time are the lists the shared files had.

**Proof that lanes merge.** On two throwaway branches from this change: on one, a fake module `zeta` (an R1 route, a database role and table, a
compose service and secret, a chart entry; its `module.yaml` plus the regenerated files); on the other, a new environment variable for AIMgF's worker
in compose and the chart. Each branch merged into the other with no conflict, in both orders, giving the same tree, and `--check` passed on it. The
branches were then deleted.

**Limits, and what is still shared.**
- Two new modules whose paths are neighbours in sort order (nothing between them) insert at the same place of a region, and git reports a conflict
  there. Resolve it by taking either side and running `python scripts/modules.py --write`: the generated files are rebuilt from the manifests.
- Not moved into the manifests, because each is a different kind of list:
  - the integration suite's in-process mesh (`tests_integration/conftest.py`, which loads apps in an order);
  - the overlays `docker-compose.{mtls,pgtls,replicas}.yml` and `values-sized.yaml` / `values-production.yaml` (deployment variants, not module
    wiring);
  - the mTLS certificate lists (`scripts/mtls_certs.py`, `scripts/compose_mtls_check.py`);
  - `scripts/init_secrets.sh`, `scripts/run_mypy.sh`, `deploy/external-secrets/`;
  - the DAST and kind-lane module lists in `.github/workflows/`.
  A module that needs one of these still edits it, `[cross-lane]`.
- AIMgF's `pyproject.toml` has a `[tool.mutmut]` configuration that nothing ran before this change, and nothing runs now (`mutation: null` keeps
  today's behaviour). Giving it a `mutation` block would add it to the pilot.
