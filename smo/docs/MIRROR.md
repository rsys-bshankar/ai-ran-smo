# The GHCR mirror of third-party images

CI and the product pull third-party images (Postgres, Grafana, Loki, Tempo, Fluent Bit, Keycloak, the Python, Node, Go and Java base images and
others). GitHub-hosted runners share outbound IP addresses, and Docker Hub limits anonymous pulls per address, so many concurrent jobs sometimes
fail with `toomanyrequests` for no fault of the change under test. The repository has no paid Docker Hub plan and does not log in to Docker Hub.
The durable fix is a copy of every pinned image in this repository's own GHCR namespace, which a job can pull without that limit.

This change builds the mirror. **It does not point anything at it yet**: the references in `docker-compose.yml`, the chart, the Dockerfiles and
the workflows are unchanged, and nothing an operator runs changes.

## How it works

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
  It does not run on a schedule: a tag without a digest in the list (see below) moves at the source, and the mirror follows it only when the workflow runs.

## What the first run found

The first run of the workflow (run 38179519262) mirrored and verified 14 of the 16 images and failed two: `sysrepo/sysrepo-netopeer2` (pinned by digest
only, mirrored as `latest`) and `drwetter/testssl.sh:3.2`. For both, `<mirror>@sha256:<source>` answered the source digest but the **tag** answered a
different one. Cause: `docker buildx imagetools create` re-wraps a single-platform image manifest into a new manifest; for a manifest list it keeps
the digest (the other 14). The verification did its job and was not loosened; the copy was changed to `skopeo copy --all --preserve-digests`. Those two
packages may carry a wrong-digest `latest` / `3.2` from that run: the next run sees the tag answer another digest, copies over it and verifies.

## The list

16 packages (`<repo>` is the repository name, in lower case):

| Package | Source images (tags) | Pinned by digest in the repository |
|---|---|---|
| `<repo>/mirror/python` | `python:3.11-slim`, `python:3.14-slim` | yes |
| `<repo>/mirror/postgres` | `postgres:18-alpine` | yes |
| `<repo>/mirror/nginx-unprivileged` | `nginxinc/nginx-unprivileged:1.31-alpine` | yes |
| `<repo>/mirror/sysrepo-netopeer2` | `sysrepo/sysrepo-netopeer2` (digest only; mirrored as `latest`) | yes |
| `<repo>/mirror/testssl` | `drwetter/testssl.sh:3.2` | yes |
| `<repo>/mirror/nuclei` | `projectdiscovery/nuclei:v3.9.0` | yes |
| `<repo>/mirror/node` | `node:26-alpine` | yes |
| `<repo>/mirror/alpine` | `alpine:3.22` | yes |
| `<repo>/mirror/golang` | `golang:1.27.0-alpine` | yes |
| `<repo>/mirror/maven` | `maven:3.9.11-eclipse-temurin-21-noble` | yes |
| `<repo>/mirror/eclipse-temurin` | `eclipse-temurin:25-jre-noble` | yes |
| `<repo>/mirror/tempo` | `grafana/tempo:2.10.8` | **no** |
| `<repo>/mirror/loki` | `grafana/loki:3.7.8` | **no** |
| `<repo>/mirror/fluent-bit` | `fluent/fluent-bit:5.1.3` | **no** |
| `<repo>/mirror/grafana` | `grafana/grafana:13.2.3` | **no** |
| `<repo>/mirror/keycloak` | `quay.io/keycloak/keycloak:26.4` (quay.io, not Docker Hub) | **no** |

Listed but not copied, because they are on `ghcr.io` already: `ghcr.io/cloudnative-pg/postgresql:17.4` and `ghcr.io/zaproxy/zaproxy:stable` (both
pinned by digest).

**Five references pin no digest** (Tempo, Loki, Fluent Bit and Grafana in `docker-compose.yml` and in the chart's `observability.*.image`, Keycloak in
`smo-gui-e2e.yml`). For these the mirror resolves the tag on the day the workflow runs and copies that digest, so the mirrored tag can be newer than what
a developer last pulled. Pinning them is a separate change (`OPEN_ITEMS.md`, `PR-OPS-12`). `tests_integration/test_image_mirror_list.py` asserts this
set exactly, so a new unpinned reference fails the test.

## One-time steps for the owner

GHCR packages are private by default, and a workflow's `GITHUB_TOKEN` cannot change that (no API offers it to it). A job cannot pull a private package
without logging in, so the references stay as they are until the packages are public.

1. Merge this change, then run the workflow once: GitHub, **Actions**, **Mirror images**, **Run workflow** on `main` (a merge that touches the list
   also runs it). Read the job summary: every one of the 16 rows should say `mirrored` or `up-to-date` (a first run says `mirrored`; the images an
   earlier run already copied and verified say `up-to-date`). A `failed` row names the reason.
2. Make each of the 16 packages above public. For each: your profile or organisation, **Packages**, the package named `<repo>/mirror/<name>`,
   **Package settings**, **Change visibility**, **Public**, and type the name to confirm. Do not skip one: a missed package fails the job that pulls it.
3. Tell whoever does the follow-up (below) that the packages are public. The check is `docker pull ghcr.io/<owner>/<repo>/mirror/postgres:18-alpine` from
   a machine that is not logged in to GHCR.

If the first run fails on a package with `denied` or `403`, open that package's settings, **Manage Actions access**, and add this repository with the
**Write** role (a package created by the workflow is linked to the repository and normally has it already).

## The follow-up change (not in this one)

After step 2, one pull request switches the references. It must change no digest:

- the Dockerfiles' `FROM`, the compose file's `image:`, the chart's `postgres.image` and `observability.*.image`, the workflows' `*_IMAGE` variables,
  the `docker run` and `services:` images and the scripts' pinned images change their repository part to `ghcr.io/<owner>/<repo>/mirror/<name>` and keep
  the tag and the `@sha256:` as they are. The mirror holds the same digest, so a pull by digest returns the same bytes;
- an operator who runs their own registry mirror can already override the chart's images (`values.yaml`); the product's defaults point at GHCR only if
  that change is wanted outside CI, which is a decision for that pull request (a Docker Hub default has the advantage of no dependency on this
  repository's packages staying public);
- `tests_integration/test_image_mirror_list.py` then reads the mirror prefix off each reference (extend `_DIGEST_REF` and the key to strip it), and
  Dependabot's ecosystems (below) keep working because they update the tag and digest in place.

No switch is built into this change: the compose file and chart pin by `name:tag@sha256:...`, and a `${SMO_IMAGE_MIRROR}` prefix would not map onto
the mirror names (`grafana/tempo` is `mirror/tempo`) without a per-image variable and a change to the chart's own image test; that is more than the
switch itself, so it waits for the pull request that needs it.

## Dependabot and the list

Dependabot's `docker` ecosystem (the Dockerfiles) and `docker-compose` ecosystem (`docker-compose.yml`) bump a pin's tag and digest in place. The mirror
list does not follow by itself, so `tests_integration/test_image_mirror_list.py` fails that pull request until `deploy/mirror/images.txt` carries
the same reference and digest. The edit is one line: copy the new `name:tag` and digest into the list (the workflow then copies the new image when the
change reaches `main`). The same test fails when a reference is removed from the repository and left in the list. Dependabot does not read workflows'
`env` image variables, the chart's values or the scripts; those are bumped by hand, and the same test catches a list that falls behind.

## What the mirror does not solve

- **Images pulled by actions themselves** (an action that runs in a container, `docker://` steps, the tools an action downloads) and by the runner image.
- **The kind node image.** `kind create cluster` pulls `kindest/node` from Docker Hub at the version the runner's `kind` binary carries; the repository pins
  none. Mirroring it needs a pin first (`--image kindest/node:<tag>@sha256:...`); it is recorded under `PR-OPS-12`.
- **The CloudNativePG operator image.** `smo-dr.yml` and `smo-tests.yml` apply the operator's release manifest, which names an image on `ghcr.io`
  (not Docker Hub); it is not in this repository, so the test cannot see it.
- **Base images no file here names**, such as an image a `docker build` pulls through a `FROM` the list does not hold. The drift test reads every
  `Dockerfile*` and fails on one not in the list.
- **Package registries** (PyPI, npm, Maven Central, Go proxy) and **other downloads** (apt, release tarballs); they have their own limits.
- **The first copy and any run of the mirror workflow itself** pulls from Docker Hub anonymously and can meet the same limit; the script retries
  (three attempts, 15 s and 30 s apart), and the workflow can simply be run again.
- **Anything while a package is private**: a job without a login cannot pull it.
- **Supply chain**: the mirror is a copy of the digest the repository already pins, so it adds no new trust in the source; it is not a scan or a
  signature check (the release images are signed, `docs/RELEASES.md`).
