# The GHCR mirror of third-party images

CI and the product pull third-party images (Postgres, Grafana, Loki, Tempo, Fluent Bit, Keycloak, the kind node image, the Python, Node, Go and Java base images and
others). GitHub-hosted runners share outbound IP addresses, and Docker Hub limits anonymous pulls per address, so many concurrent jobs sometimes
fail with `toomanyrequests` for no fault of the change under test. The repository has no paid Docker Hub plan and does not log in to Docker Hub.
The durable fix is a copy of every pinned image in this repository's own GHCR namespace, which a job can pull without that limit.

Two steps built it. **Step 1** (`PR-OPS-12.1`, `12.4`) built the mirror: the list, the copy script, the workflow, and a digest on every pin. **Step 2**
(`PR-OPS-12.3`, this document's "CI uses the mirror" section) makes CI pull from it. **Step 2 is CI only: the defaults an operator runs name the upstream images,
as before** (`docker-compose.yml`, the chart's `values.yaml`, the Dockerfiles' `FROM`, the GitOps overlays, the production overlay). Public GHCR is not a runtime
dependency of anyone's install; `tests_integration/test_ci_image_mirror.py` fails if any of those files names the mirror.

## How the mirror is filled

- **`deploy/mirror/images.txt`** is the list: one line per image, `source | digest | mirror | note` (the file's header explains the columns).
  `source` is the reference as the repository writes it, `digest` the `sha256:` the repository pins (`-` where it pins none), `mirror` the name under
  `ghcr.io/<owner>/<repo>/mirror/`. The mirror's tag is the source's tag (`latest` for a source with none).
- **`scripts/mirror_images.py`** reads the list and has three commands: `validate` (no network; every entry has a digest or a reason, no duplicate
  source, no two entries on the same mirror tag, valid names), `packages --prefix ...` (the package names) and `sync --prefix ...` (copy).
  `sync` copies each image **by digest** with `skopeo copy --all --preserve-digests docker://<source>@<digest> docker://<mirror>:<tag>`, which copies
  every architecture and writes the source's manifest bytes unchanged, so the digest is the same. It skips an image whose mirror tag already answers
  with that digest (so a re-run changes nothing); a tag that answers a **different** digest is not "present" and is copied over. After a copy it checks
  that `<mirror>@<digest>` and `<mirror>:<tag>` both answer with the source's digest; a different digest fails the entry. If skopeo cannot preserve a
  digest (an old schema 1 manifest, a recompression) it fails that entry rather than push another digest.
  A copy that fails (the source's own pull limit, a network error) is retried three times with a growing pause; one failed image does not stop the
  others, and the run exits 1 if any failed.
- **Tool: `skopeo`**, which GitHub's `ubuntu-latest` runner image carries (its software list names Skopeo with Podman and Buildah), so nothing is
  installed and nothing is downloaded unpinned. It logs in to the destination with `--dest-authfile`, the file `docker login ghcr.io` wrote
  (`~/.docker/config.json`, the script's `--authfile`); no token is on a command line, and the workflow still passes the token to `docker login`
  through the environment. The source is read anonymously. **Why not `docker buildx imagetools create`:** for a source that is a manifest list it keeps
  the digest, but for a source that is a single-platform image manifest it wraps the manifest in a new one, so the tag gets a different digest than
  `@sha256:` pins in the repository name. The script therefore uses skopeo; if skopeo is not found it still copies a manifest-list source with
  `imagetools create` (the type is read from `docker buildx imagetools inspect --raw`) and fails a single-platform source with a message saying
  skopeo is needed. `crane` is not used: it would be a download to pin by version and checksum.
- **`.github/workflows/mirror-images.yml`** runs the script on `workflow_dispatch` (input `dry_run`: resolve and compare only) and on a push to `main`
  that changes the list, the script or the workflow. It has no `pull_request` trigger, so a fork cannot reach the token that writes packages. Its
  permissions are `contents: read` and, in the one job, `packages: write`; it logs in to `ghcr.io` with `GITHUB_TOKEN`, validates the list, runs `sync`,
  and writes a table of the mirrored images and the package names below to the job summary. Its only action is `actions/checkout`, pinned by SHA; the job installs nothing.
  It does not run on a schedule: a tag without a digest in the list moves at the source, and the mirror follows it only when the workflow runs (the list holds none today).

The first run of the workflow (run 38179519262) mirrored and verified 14 of the 16 images then listed and failed two: `sysrepo/sysrepo-netopeer2` (pinned by digest
only, mirrored as `latest`) and `drwetter/testssl.sh:3.2`. For both, `<mirror>@sha256:<source>` answered the source digest but the **tag** answered a
different one. Cause: `docker buildx imagetools create` re-wraps a single-platform image manifest into a new manifest; for a manifest list it keeps
the digest (the other 14). The verification did its job and was not loosened; the copy was changed to `skopeo copy --all --preserve-digests`.

## The list

17 packages (`<repo>` is the repository name, in lower case). The owner made all of them public (`OPS-12.2`) and checked that every digest-pinned source answers its pinned
digest at the mirror.

| Package | Source images (tags) | Pinned by digest in the repository |
|---|---|---|
| `<repo>/mirror/python` | `python:3.11-slim`, `python:3.14-slim` | yes |
| `<repo>/mirror/postgres` | `postgres:18-alpine` | yes |
| `<repo>/mirror/timescaledb` | `timescale/timescaledb:2.31.0-pg18` (the Contracts job's scratch database, plan task S0.7) | yes |
| `<repo>/mirror/nginx-unprivileged` | `nginxinc/nginx-unprivileged:1.31-alpine` | yes |
| `<repo>/mirror/sysrepo-netopeer2` | `sysrepo/sysrepo-netopeer2` (digest only; mirrored as `latest`) | yes |
| `<repo>/mirror/testssl` | `drwetter/testssl.sh:3.2` | yes |
| `<repo>/mirror/nuclei` | `projectdiscovery/nuclei:v3.9.0` | yes |
| `<repo>/mirror/node` | `node:26-alpine` | yes |
| `<repo>/mirror/alpine` | `alpine:3.22` | yes |
| `<repo>/mirror/golang` | `golang:1.27.2-alpine` | yes |
| `<repo>/mirror/maven` | `maven:3.9.11-eclipse-temurin-21-noble` | yes |
| `<repo>/mirror/eclipse-temurin` | `eclipse-temurin:25-jre-noble` | yes |
| `<repo>/mirror/tempo` | `grafana/tempo:2.10.10` | yes |
| `<repo>/mirror/loki` | `grafana/loki:3.7.8` | yes |
| `<repo>/mirror/fluent-bit` | `fluent/fluent-bit:5.1.5` | yes |
| `<repo>/mirror/grafana` | `grafana/grafana:13.2.4` | yes |
| `<repo>/mirror/keycloak` | `quay.io/keycloak/keycloak:26.4` (quay.io, not Docker Hub) | yes |
| `<repo>/mirror/kind-node` | `kindest/node:v1.37.0` (the node image of every `kind create cluster`) | yes |

Listed but not copied, because they are on `ghcr.io` already: `ghcr.io/cloudnative-pg/postgresql:17.4` and `ghcr.io/zaproxy/zaproxy:stable` (both
pinned by digest).

**Every third-party reference pins a digest** (`PR-OPS-12.4`). Tempo, Loki, Fluent Bit and Grafana (`docker-compose.yml` and the chart's
`observability.*.image`, which move together) and Keycloak are `name:tag@sha256:...` in the repository, with the digest of each tag's manifest list (index), so
the mirror copies exactly what the repository pulls. The kind node image is pinned too: the workflows install no `kind`, they use the binary on the runner
image (kind 0.33.0), whose default node is `kindest/node:v1.37.0@sha256:a1ed56cf...`; every `kind create cluster` passes that node (since step 2 through the
list, below), so a newer runner image cannot move the cluster's Kubernetes version without a reviewed edit. `tests_integration/test_image_mirror_list.py` fails on an
unpinned reference (its known-unpinned set is empty), on a `kind create cluster` with no `--image "$SMO_IMAGE_KIND_NODE_..."`, and on a list entry nothing uses any more.

## CI uses the mirror (`PR-OPS-12.3`)

### The mechanism

One script, `scripts/ci_mirror.py`, and one step at the start of every job that pulls an image (`Use the GHCR image mirror`, which runs `python3 scripts/ci_mirror.py setup`
with `SMO_IMAGE_MIRROR: ${{ vars.SMO_IMAGE_MIRROR }}` as its only environment). The pins stay in `deploy/mirror/images.txt`; no workflow repeats a digest.

`setup` asks the registry, anonymously (the packages are public; no `docker login`), whether `<mirror>@<digest>` answers the pinned digest, for every mirrored row, a few at a time and with
a 60 s limit per call. An image that answers is **switched**; one that does not (a package that is private again, GHCR unreachable, a repository without the packages such as a fork) is **not**
switched and stays on its upstream registry. For each one `setup` prints `::warning::GHCR image mirror: <image> is not used, this job pulls it from its upstream registry: <why>`, which shows as an
annotation of the run. It never fails the job for a mirror problem. Then it writes, under `smo/.ci-mirror/` (git-ignored; never committed; not a default of anything):

| File | What it holds | Used by |
|---|---|---|
| `state.json` | the switched references (upstream reference to mirror reference) and the failures | `ci_mirror.py image`, `build-contexts` |
| `docker-compose.mirror.yml` | a compose override: `image:` of each service whose image is switched, `build.additional_contexts` for each switched `FROM` of a service's Dockerfile; `services: {}` when nothing is | `COMPOSE_FILE` / `-f` |
| `helm-values.yaml` | the chart's switched `image:` values (`postgres.image`, `observability.*.image`); `{}` when nothing is | `helm install|upgrade -f` |

and appends one variable per list row to `$GITHUB_ENV`: `SMO_IMAGE_<NAME>`, the mirror reference when switched and the upstream `repo:tag@digest` otherwise (`python:3.11-slim` is
`SMO_IMAGE_PYTHON_3_11_SLIM`, `postgres:18-alpine` is `SMO_IMAGE_POSTGRES_18_ALPINE`, `kindest/node:v1.37.0` is `SMO_IMAGE_KIND_NODE_V1_37_0`; the name is the mirror name, or the last path component
of an unmirrored source, plus the tag). **Every reference keeps the pinned digest**: the mirror reference is `<prefix>/<name>:<tag>@sha256:<the same digest>`, so a pull returns the bytes the
repository pins. Tag and digest together is the form `kind` and `docker` already accept; the digest decides.

### What is switched, by kind

| Kind | Where | How |
|---|---|---|
| **Compose lanes** (`docker compose up --build`, `run`) | `smo-tests.yml` (`compose-e2e`, `compose-pooler`, `compose-replicas`, `obs-stack`, `compose-mtls`, `compose-pgtls`, `netconf-lab`), `deploy-on-main.yml`, `smo-dast.yml` (both jobs), `smo-gui-e2e.yml` (both), `smo-load.yml`, `smo-soak.yml`, `smo-stress.yml` (both), and `compose_new` of `scripts/check_upgrade_from_previous_release.sh` | The generated override is named in the job's `COMPOSE_FILE` (`docker-compose.yml:.ci-mirror/docker-compose.mirror.yml`, plus the lane's own overlay) or, in a lane that passes `-f`, as one more `-f`. Third-party services (`postgres`, `edge-tls`, `netconf-lab`, `tempo`, `loki`, `fluent-bit`, `grafana`) get `image:`; every service built from the root, `gui`, `pgbouncer` or `backup` Dockerfile gets named contexts |
| **`docker build` of module images** | `image-scan.yml`, `smo-ha-kind.yml` (both), `smo-upgrade-kind.yml`, `smo-tests.yml` (`helm`, `postgres-ha`, `sdk-java`'s example) | `mapfile -t mirror < <(python3 scripts/ci_mirror.py build-contexts <dir>)` then `docker build ... "${mirror[@]}" ...`: one `--build-context '<FROM text>=docker-image://<mirror reference>'` per switched `FROM` (BuildKit named contexts replace the base image). The key is the whole `FROM` reference, digest included: a key without the digest does not match a digest-pinned `FROM` (shown with a local BuildKit and with `docker compose build` while writing this) |
| **`docker run`** | `deploy-on-main.yml`, `smo-tests.yml`, `smo-load.yml`, `smo-soak.yml`, `smo-stress.yml`, `smo-dast.yml` (testssl.sh, Nuclei), `smo-gui-e2e.yml` (Keycloak), `smo-db-volume.yml`, `smo-dr.yml` (Postgres), and the demo consumer of both previous-release scripts | `"$SMO_IMAGE_..."` in place of the literal image |
| **Service containers** | `smo-tests.yml` `migration-postgres` | A `services:` image is pulled by the runner before any step, so neither the mirror nor the fallback can be chosen. The Postgres is a `docker run -d` step now, with the same user, database, port and health check. A test fails if any workflow uses `services:` or `container:` |
| **`kind create cluster --image`** | all six calls (`smo-tests.yml` `helm` and `postgres-ha`, `smo-ha-kind.yml` `ha` and `hpa`, `smo-upgrade-kind.yml`, `smo-dr.yml` `cnpg-wal-archive`) | `--image "$SMO_IMAGE_KIND_NODE_V1_37_0"`; kind pulls the node image through the runner's Docker, which pulls the public package |
| **Helm installs on kind** | `smo-tests.yml` `helm` (one install, four upgrades), `smo-ha-kind.yml` (two installs) | `-f .ci-mirror/helm-values.yaml`, after `kind-values.yaml`. The chart exposes the image fields, so nothing in the chart changes. The same file goes on every upgrade of a release, so the database's image text never changes mid-test |

### The fallback

The decision is made once per job, per image, when `setup` runs: an image whose mirror reference does not answer its digest is simply not switched, and the rest of the job is exactly the
pre-mirror job for it (Docker Hub, with its limit). The warning names the image and the reason. The fallback does **not** cover a mirror image that answered the manifest request and then fails
mid-job (a blob that cannot be fetched): that pull fails the step, and the repair is the kill switch below. Release builds do not have a fallback to need: they never use the mirror.

### The kill switch

Set the repository variable `SMO_IMAGE_MIRROR` to `off` (GitHub, Settings, Secrets and variables, Actions, Variables; `false`, `0` and `no` also work, any case). Every job's `setup` then
switches nothing, prints a notice, and the job runs against the upstream references as before step 2. Unset, empty or any other value leaves the mirror on. The variable is read in exactly one
place per job (the `setup` step's `env`), and `tests_integration/test_ci_image_mirror.py` holds it there.

### Adding or bumping an image

- **A Dependabot bump of a pin** (compose file, Dockerfile) changes the tag and digest in place. `test_image_mirror_list.py` then fails until `images.txt` carries the same `name:tag` and
  digest (the one-line edit); on merge to `main` the mirror workflow copies the new image; from then on CI pulls it from the mirror. Until that workflow has run, the new digest does not answer at the
  mirror, so `setup` leaves that one image on Docker Hub with a warning (the fallback working as designed) instead of failing. Workflows hold no digest, so there is nothing else to bump. A bump of
  the kind node, Keycloak, testssl.sh or Nuclei is one edit of `images.txt` (and, for kind and Keycloak, nothing else); Dependabot does not read those.
- **A new third-party image** in a Dockerfile, compose file or chart needs a row in `images.txt` (the drift test says so); a new `docker run` takes it as `"$SMO_IMAGE_<NAME>"`, a new compose
  service needs nothing (the override is generated from `docker compose config`), a new job that pulls anything starts with the `Use the GHCR image mirror` step. The tests in
  `test_ci_image_mirror.py` fail on a job without it, a `docker run` with a literal image, a `docker compose up` that does not reach the override, a `docker build` without the contexts, a
  `helm install|upgrade` without the values file, and a workflow that names a mirrored image.

### Not covered

What stays on its upstream registry, and why. The keys in backticks are the names `tests_integration/test_ci_image_mirror.py` uses for its exceptions.

- `workflow:release-images.yml`: release images are built from the upstream references on purpose, so a release stays reproducible without any package of this repository (the build is `docker buildx build`
  with no named contexts). Only CI uses the mirror.
- `workflow:mirror-images.yml`: it fills the mirror, so it reads the sources from their own registries (and can meet the same pull limit; the script retries three times, and the workflow can be run again).
- `docker-run:ZAP_IMAGE`: the ZAP image is `ghcr.io/zaproxy/zaproxy:stable@sha256:...`, already on ghcr.io (no Docker Hub limit); `smo-dast.yml` pins it itself and the list does not copy it.
- `helm:smo-upgrade-kind.yml`: the upgrade lanes install a previous release's chart and then this checkout's; the two charts' database image would differ in text if only one were mirrored, and a changed
  image restarts Postgres in the middle of the upgrade being tested. That lane's cluster pulls the database image from Docker Hub. Its `kind` node, and the module images it builds from this checkout and
  from the previous release's source, do use the mirror where the previous release's `FROM` is a listed digest (a base image the list does not hold is simply built from upstream).
- `helm:cnpg`: the CloudNativePG installs (`smo-tests.yml` `postgres-ha`) run no chart database; the operator and the operand are on `ghcr.io`.
- **The previous release's own stack** (`check_previous_release_code.sh`, and the previous half of `check_upgrade_from_previous_release.sh`): it builds and pulls the pins of an older tag, which the
  mirror does not hold. Only the demo consumer container and, in the upgrade script, this commit's own stack use the mirror.
- **Images pulled by actions themselves** (`ossf/scorecard-action`, `google/clusterfuzzlite`, `actions/dependency-review-action` and the tools an action downloads), by the runner image, and by tools
  the jobs install (`go install` of Trivy and its vulnerability database from `public.ecr.aws` / `ghcr.io`, the `metrics-server` and CloudNativePG manifests, which name images on `registry.k8s.io` and
  `ghcr.io`).
- **Package registries** (PyPI, npm, Maven Central, Go proxy) and **other downloads** (apt, release tarballs); they have their own limits.
- **A mirror image that answers the manifest and then fails mid-job**: see "The fallback".
- **Supply chain**: the mirror is a copy of the digest the repository already pins, so it adds no new trust in the source; it is not a scan or a
  signature check (the release images are signed, `docs/RELEASES.md`).

### What was exercised before merge, and what first runs in CI

Run locally: the list and drift tests, `test_ci_image_mirror.py` (the generator and `setup` against a fake registry, the workflow rules over every workflow), `docker compose config` with the
generated override merged into `docker-compose.yml`, `docker compose build` and `docker build --build-context` against a local BuildKit (named contexts replace a digest-pinned `FROM` only when
the key carries the digest; tag and digest together in `docker-image://` works), `helm template` with the generated values. **First exercised by CI** (a workflow cannot run here): the anonymous
manifest probe against the real GHCR packages from a GitHub runner, a real pull of every mirror image by compose, `docker build`, `docker run` and the kind node, the converted Postgres step of
`migration-postgres`, `kind create cluster --image <mirror>:<tag>@<digest>`, and the Helm installs with the generated values on a kind cluster.
