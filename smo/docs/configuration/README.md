# Configuration reference

One file per group (`<group>.md`, generated, and `<group>.json`, its descriptions), so a pull request that adds a variable to a module
changes that module's files only (`smo/CLAUDE.md`, "Working in parallel"). The index of the groups is at the end.

Every environment variable the SMO reads, with its default, whether it is a secret, which files read it and what it does. The table is
generated from the source by `scripts/config_reference.py` (an AST walk, nothing is imported) and the descriptions are written by hand in
`<group>.json` beside each table (this directory); `tests_integration/test_config_reference.py` fails when a variable read in code is missing from the table, when the
table lists one no code reads, when a default drifted, or when a description is missing. To add or change a variable: change the code, add its
description to the JSON file of its group (`aimgf.json` for a variable only AIMgF reads), run

```bash
cd smo && python scripts/config_reference.py --write     # regenerates the group files and the index below; --module <group> for one (python scripts/config_reference.py: a plain table with file:line)
```

and commit both. A variable whose name is built at run time is listed as a pattern, the run-time part in angle brackets
(`NETCONF_CRED_<REF>_PASSWORD`: one per credential name an endpoint refers to); such a read carries a `# config-ref: NAME, ...` comment in the source
so the walk knows what it reads.

## How to read the tables

- **Default**: the value used when the variable is unset, as far as the code shows it. `unset` means the code treats absence as "not
  configured" (an optional feature, or a required value that the deployment must supply); `required` means the process fails without it; `computed in code`
  means the default is built from other values (the description says how). Several defaults separated by `/` mean the variable is read in several places with
  different defaults.
- **Secret**: `yes` for a variable that holds a secret (the name contains PASSWORD, SECRET, KEY or TOKEN, or it is read through `read_secret`, and the
  description may correct the guess); `file` for the `*_FILE` form of one (the path of a file that holds it). Never put a secret in a values file
  or an image; use the file form.
- **Read in**: the source files that read it. A variable read in `smo_shared` is read by every module that uses that part of the library.
- Variables are grouped by where they are read: `smo_shared` (every module), one module, or several modules; the last group is variables only
  `docker-compose.yml` substitutes from `.env`.

## The `*_FILE` convention

A secret can be given as a file instead of a value (`smo_shared/secretfile.py`): `read_secret("SMO_DATABASE_PASSWORD")` returns the value of
`SMO_DATABASE_PASSWORD`, or the contents of the file named by `SMO_DATABASE_PASSWORD_FILE` (one trailing newline removed). Neither set is "not
configured"; both set is an error (`SecretConflict`) rather than a guess; an unreadable file names the variable and the path, never the contents. An
environment variable is visible to `docker inspect` and `/proc/<pid>/environ`, a file mounted from a Compose secret or a Kubernetes Secret is not, so
compose and the chart use the file form (`docs/SECRETS.md` lists each secret, its owner and its rotation). The GUI backend's image does not install
`smo_shared` and has its own copy of this rule for `SMO_ENROLLMENT_SECRET`. The `*_FILE` rows below are the variables that name such files.

## Where each variable is set in a deployment

- **Docker Compose** (`docker-compose.yml`): each service's `environment:` block sets the module's variables (the shared database block `x-db-env`
  sets `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` and `SMO_DB_POOLER`); a `${NAME:-default}` in the file is substituted from your shell or from `.env` (copy
  `.env.example`), and those names are the "Compose only" group and the variables marked in the tables. A secret is a Compose secret mounted under
  `/run/secrets` and named by a `*_FILE` variable. The image's build arguments `MODULE`, `SMO_VERSION`, `SMO_BUILD_SHA` and `SMO_BUILT_AT` are set at build time
  (`x-build-info`), not at run time.
- **Helm** (`deploy/helm/smo/values.yaml`, described in `deploy/helm/smo/README.md`): the chart sets the database, secrets and enrollment variables itself
  from `postgres`, `secrets` and `databaseRoles`; per-module variables go under `modules.<name>.env` (merged over `moduleDefaults.env`), the GUI backend's
  under `gui.env`, and a few have a value of their own (`ingress.r1.publicBaseUrl` is `R1_PUBLIC_BASE_URL`, `rappCredentials` drives
  `RAPP_CREDENTIAL_DELIVERY` and the `RAPP_K8S_*` variables). Any variable in the tables can be added to a module's `env` map.
- **Not covered here**: the variables of third-party images (`POSTGRES_*`, PgBouncer's own, nginx) are documented by those projects; this file lists what
  the SMO code reads and what `docker-compose.yml` substitutes.

## Groups

<!-- BEGIN GENERATED: scripts/config_reference.py --write -->

| Group | Variables of |
|---|---|
| [`shared.md`](shared.md) | Every module (`smo_shared`) |
| [`several.md`](several.md) | Read by several modules |
| [`aimgf.md`](aimgf.md) | the module `aimgf` |
| [`coverage-optimization-rapp.md`](coverage-optimization-rapp.md) | the module `coverage-optimization-rapp` |
| [`dme.md`](dme.md) | the module `dme` |
| [`echo.md`](echo.md) | the module `echo` |
| [`energy-saving-rapp.md`](energy-saving-rapp.md) | the module `energy-saving-rapp` |
| [`focom.md`](focom.md) | the module `focom` |
| [`gui-bff.md`](gui-bff.md) | the module `gui-bff` |
| [`mdaf.md`](mdaf.md) | the module `mdaf` |
| [`mobility-optimization-rapp.md`](mobility-optimization-rapp.md) | the module `mobility-optimization-rapp` |
| [`mock-o1-adaptor.md`](mock-o1-adaptor.md) | the module `mock-o1-adaptor` |
| [`onboarding.md`](onboarding.md) | the module `onboarding` |
| [`r1-termination.md`](r1-termination.md) | the module `r1-termination` |
| [`ran-nf-oam.md`](ran-nf-oam.md) | the module `ran-nf-oam` |
| [`rapp-mgmt.md`](rapp-mgmt.md) | the module `rapp-mgmt` |
| [`sa-smos.md`](sa-smos.md) | the module `sa-smos` |
| [`scripts.md`](scripts.md) | the module `scripts` |
| [`sdk.md`](sdk.md) | the module `sdk` |
| [`sme.md`](sme.md) | the module `sme` |
| [`teiv.md`](teiv.md) | the module `teiv` |
| [`traffic-steering-rapp.md`](traffic-steering-rapp.md) | the module `traffic-steering-rapp` |
| [`compose.md`](compose.md) | Compose only (`.env`, substituted into `docker-compose.yml`) |

<!-- END GENERATED -->
