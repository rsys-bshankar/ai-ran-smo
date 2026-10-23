# Data residency: where data lives, and what leaves a site

One page for an operator deciding where to run this and what to allow out (`STD-6.1`). Written from the compose file, the Helm chart and the code (October 2026). The platform has no
cloud component of its own: everything below runs where you start it. Personal data in particular is inventoried in [`PRIVACY.md`](PRIVACY.md).

## Where data lives

| Data | Where | Notes |
|---|---|---|
| Every module's state: managed entities, alarms, performance files, configuration jobs and snapshots, models, intents, SME registrations, the outbox, the platform audit chain (`audit_log`), `module_identity` credentials (encrypted at rest when `SMO_IDENTITY_KEY` is set; the key is not in the database) | **Postgres 18**, volume `smo_pgdata` in compose; the database of the Helm release (a PVC, or CloudNativePG in the HA lab) | One database, one schema per module (`DB-2`). Postgres can be an external service: `SMO_DATABASE_URL`, `SMO_DB_HOST` |
| GUI users, the GUI audit log, failed-login counters, revoked sessions, the session signing key (when `GUI_JWT_SECRET` is unset), the BFF's SME credential, the first-start admin password file | **SQLite file on volume `gui_bff_data`** (`GUI_DATABASE_URL`, default `sqlite:////data/gui-bff.db`); any SQLAlchemy URL works | Not in Postgres and not in the `db_backup.sh` dump; the compose `db-backup` service copies it into each off-site set (`docs/DISASTER_RECOVERY.md`), nothing does on Kubernetes. Replicas of the BFF need one shared `GUI_DATABASE_URL` |
| Onboarded rApp packages (CSAR files) | volume `smo_packages` (`/srv/packages`, Onboarding) | |
| CSAR files an operator copies in to be served by location | volume `smo_scratch` (`/srv/scratch`, R1 Termination) | |
| Scratch space | `/tmp` of each container: memory, gone on restart (the root filesystem is read-only) | |
| Secrets | files in `secrets/` on the Docker host (`db_password`, `enrollment_secret`, `tls_cert`, `tls_key`), mounted into the containers; Kubernetes Secrets in Helm | `docs/SECRETS.md` lists each one and whether it is stored as a hash or in plaintext |
| Backups | wherever the operator puts them: `scripts/db_backup.sh` writes `smo/backups/smo-<UTC>.dump` (mode 0600) on the host that runs it | Off-site copies: with `scripts/dr_backup.sh` (compose service `db-backup`, profile `backup`) every database dump, the GUI SQLite file and a manifest go to the S3-compatible bucket named by `SMO_BACKUP_S3_BUCKET`, which can be in another country than the site; with CloudNativePG `postgres.cnpgBackup` and the cluster's `barmanObjectStore` send the WAL and base backups to the bucket the operator names. Choose its region with `docs/DISASTER_RECOVERY.md` in mind. Nothing is sent unless one of those is configured |
| Logs | stdout of each container, as the container runtime keeps them; no log shipper is installed (`OBS-6`, not built) | Structured logs carry route templates and correlation ids, not raw paths or queries; the GUI web server's access log carries client IP addresses and request paths (`PRIVACY.md`, rows 15 and 16) |
| Container images | the registry the deployment pulls from. Compose builds from the checkout; Helm pulls `ghcr.io/rsys-bshankar/ai-ran-smo/smo-<name>:<tag>` (value `image.registry`) | Pinned base images by digest: Postgres, nginx, Python, node |
| Performance data content, alarm text | inside Postgres (`pm_file.content`, `alarm`), as the network sent it | The platform does not inspect it (`PRIVACY.md`, section 1) |

## What leaves a site

Everything in this table is initiated by the platform at run time. Nothing else is: **there is no telemetry, analytics, crash reporting, licence check or update check in the code**
(searched: no client for any such service; the GUI loads no external script, font or image and its content security policy allows `self` only; OpenTelemetry export is not built, `OBS-3`).

| What | Where to | When | Controlled by |
|---|---|---|---|
| **Notifications to caller-registered URLs** (outbox: DME, SME, AIMgF, FOCOM, MDAF, Intent Service, RAN NF OAM; and the two inline reads, a producer's health URL and an O1 adaptor's `/capabilities`) | The URL the subscriber registered: anywhere the subscriber chose. The payload is what the module puts in the notification | On the event, with retries (5 attempts, backoff) | The callers who register destinations. `smo_shared.webhook` refuses non-http(s) URLs and literal loopback, link-local (including the cloud metadata address), multicast and reserved addresses, at registration and again at send time. At send it also resolves the host name, refuses the call when any address it resolves to is blocked, and connects only to the addresses it checked (`SEC-15.16`). Every other host, private ranges included, is allowed: there is no allow-list of destination hosts. `docs/NOTIFICATIONS.md` lists every call site |
| **Package download** | The `location` URL of a package an operator or rApp asks Onboarding to onboard (`.csar`, 30 s timeout, same URL guard) | On onboarding | Who may call it (operator in the GUI) |
| **O1 southbound** | The adaptor or network element registered as an `o1_adaptor_endpoint`: NETCONF over SSH (port as registered) or over TLS with a client certificate, or RESTCONF over plain HTTP, and the adaptor's HTTP endpoint for the legacy transport | When a configuration job, software-management job or CM read runs | The endpoints an admin registers; the credentials by reference (`docs/SECRETS.md`) |
| **O2 southbound** | **None today.** FOCOM keeps its own inventory and provisioning records, and NFO instantiates nothing on a real cluster: neither makes an outbound O2 call | – | – |
| **Kubernetes API** | The cluster's own API server, only with `RAPP_CREDENTIAL_DELIVERY=kubernetes` (Helm `rappCredentials.delivery`): rApp Management writes a Secret per rApp instance | On instance create, rotate and terminate | The Helm value |
| **Image pulls** | The registry named above (GHCR for the released images), and, when building from the checkout, Docker Hub for base images, PyPI (hash-locked, `requirements/`), the Debian mirror (one `apt-get` in the `Dockerfile`) and the npm registry (GUI build) | At pull or build time, not at run time | The operator's registry mirror and network policy |
| **Signatures and provenance** | Nothing leaves a site to verify an image: the cosign signature, the SLSA provenance and the SBOM sit in the registry beside each image (`.github/workflows/release-images.yml`; signing is keyless, so the signature names that workflow as signer). Verifying them is an operator step against the registry and the public Sigstore services | At release (CI), and when an operator verifies | The operator |
| **Postgres and Prometheus** | Postgres `:5432` is published on the host in compose (`SECURITY.md`); each module's `/metrics` is for a scraper on the container network and is not served by the TLS edge | – | The operator |

## What an operator controls

- **Where it runs.** Nothing in the stack needs an Internet connection to run once the images and the database exist. An air-gapped site mirrors the images and sets `image.registry`.
- **What is published.** Compose publishes R1 `:8080`, the GUI `:3000` and Postgres `:5432`; the `tls` profile adds `:8443` and `:3443`. Unpublish the plain ports and Postgres for anything but a laptop.
- **Which destinations can be called.** Only by deciding who may register a notification URL or an O1 endpoint (RBAC in the GUI; at R1 an rApp may register subscriptions, not O1 endpoints). A network egress policy is the real boundary: the platform's own URL guard is a safeguard, not an allow-list.
- **Retention.** Built for alarms, PM files, safeguard refusals, MDAF reports and the GUI audit log, and off until the operator sets a number of days: `docs/RETENTION.md`. Other tables: see `PRIVACY.md` section 2. Otherwise the operator deletes what must go.
- **Backups and logs**, their location (the off-site bucket included, `docs/DISASTER_RECOVERY.md`), encryption and expiry; the platform writes them in plain files and standard output.
- **Region.** A managed element carries a `region` and a `tenant` (`managed_entity`, `SEC-10.2`), and an rApp or console user with a scope claim may touch only the elements of its regions and tenants (`smo_shared/scope.py`, `docs/adr/0005-tenant-region-authorization.md`). That decides who may see and change what; it does not decide where data is kept. Data of every region is stored in the same database, and the stores and outbound flows above are not split by region. A deployment per region is the way to keep data in one.
