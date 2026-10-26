# Releases

How the SMO is versioned and released (`PR-OPS-4.1`). The release history is `CHANGELOG.md`.

## Tag scheme

Semantic Versioning 2.0.0, with a **`smo-v`** prefix because this repository also holds the specification material outside `smo/`:

    smo-v<MAJOR>.<MINOR>.<PATCH>            smo-v0.1.0, smo-v1.4.2
    smo-v<MAJOR>.<MINOR>.<PATCH>-rc.<N>     release candidates, smo-v1.0.0-rc.1

One version for the whole platform: every module image, the shared library, the SDK and the Alembic history are released together.
Until `1.0.0`, `0.MINOR` is the breaking-change number and `0.x.PATCH` is for fixes.

## What each number means

| Bump | When |
|---|---|
| **MAJOR** | An incompatible change to an interface an operator or an rApp depends on: an R1 route, request or response field removed or changed in meaning; an rApp package (CSAR) manifest change old packages fail; a configuration variable removed or repurposed; a schema revision that cannot be applied while the previous release still runs, or has no supported upgrade path from the previous MAJOR's last release |
| **MINOR** | Backwards-compatible addition: a new route, field, module, configuration variable (with a default that keeps the old behaviour) or an additive schema revision |
| **PATCH** | A fix that changes none of the above: a bug, a security fix, a dependency bump, documentation |

A schema revision is additive when the previous release's code runs on the new schema (a new nullable column, a new table, a new index). Anything else needs
the MAJOR bump, or an expand/contract split across two MINOR releases (`PR-OPS-5`).

## Compatibility checks and the deprecation policy

CI compares the contract with the previous release on every pull request (`scripts/check_breaking_changes.py`, job "R1 contract has no breaking change since the previous release"): each `docs/openapi/<module>.json` at the newest `smo-v*` tag against the one in the checkout. A removed operation, a new required parameter or request property, a narrowed bound or enum, a changed type or a lost response property fails the job. A base it cannot use is an error, never a pass: no release tag, a `--base` that does not resolve to a commit, or a base with no `docs/openapi` documents all exit non-zero with a message (the job fetches tags and full history so that the newest tag exists). An intended break is waived in `scripts/breaking_change_waivers.json` with a reason (a key may use `*`); a waiver that matches nothing fails, and the file is emptied right after a release is tagged ("Cutting a release", step 4), since each waiver describes the comparison with one release. Schema compatibility has its own jobs (the previous release's code on the new schema; the upgrade that keeps data).

Deprecation, for a MAJOR-bump change made without surprising a consumer:

1. **Announce first.** A route, field or setting to be removed or narrowed is marked deprecated in a MINOR release: `deprecated: true` in its OpenAPI entry, a `### Deprecated` entry in `CHANGELOG.md` naming what replaces it and the release it goes in.
2. **Keep it working for at least one MINOR release** (two before 1.0), and answer a call to it as before; where a response can carry it, add a `Deprecation` header.
3. **Remove it only in a MAJOR release** (before 1.0, in a MINOR release that says so under `### Changed` with an upgrade note), and then waive it in the break file in the same pull request.

A change that only refuses input that used to make the service fail (a 500) is a fix, not a break; it is waived with that reason.

## Criteria for 1.0.0

`1.0.0` is cut when all of these hold on the release candidate (`smo-v1.0.0-rc.N`); each has a place where its evidence lives.

1. **Every validation lane is green** on the candidate: unit, integration, contract, breaking-change, migration, upgrade (from the previous two releases) and rollback, HA, DB-volume, load, stress, GUI browser and DAST lanes (`docs/VALIDATION.md`).
2. **A 72 hour soak** on a runner of our own with memory, connections, descriptors and outbox depth flat (`scripts/soak_check.py`; the result is attached to the release).
3. **The external penetration test is passed**: scope in `docs/PENTEST_SCOPE.md`, findings rated high or critical fixed, the report summarised in the release notes.
4. **One full disaster-recovery drill** with timings inside the targets (RPO 15 minutes, RTO 1 hour), from an off-site copy (`PR-HA-6`): the runbook of `docs/DISASTER_RECOVERY.md` run on a spare host or namespace at a representative database size, with the line added to its drill log. The CI job `disaster-recovery` proves the mechanism and the gates at small size, and does not satisfy this by itself.
5. **Operations documents are complete**: configuration reference (`PR-OPS-7`) with its CI check, a runbook entry for each alert (`PR-OBS-7`), measured sizing (`PR-OPS-9`), SLIs and SLOs (`PR-OBS-5`).
6. **Traffic between services is mutually authenticated**: native mTLS (`PR-SEC-2`, built in 0.5.0, opt in) with the rotation procedure tried once (the CI job `compose-mtls` renews and restarts three services; the CA rotation phases are tested by handshake). The service-mesh path (`PR-SEC-3`) was decided against, so it is not a way to meet this criterion.
7. **No unwaived breaking change since 0.5.0**: `scripts/breaking_change_waivers.json` is empty at the candidate, and the deprecation policy above has been followed for everything removed.
8. **The compliance documents exist and are current**: spec release table, personal-data inventory and erasure procedure, data residency statement, control matrix (`PR-STD`).

A criterion that cannot be met is not waived silently: the release notes say which one and why, and the person cutting the release decides.

## Cutting a release

1. Everything for the release is merged to `main` and CI is green on it.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, start a fresh `## [Unreleased]` above it, and update the compare links at the foot. Merge that as its own PR (`Release X.Y.Z`). Leave `smo/scripts/breaking_change_waivers.json` as it is: until the tag exists the check still compares with the previous release, and an empty file there fails the required job "R1 contract has no breaking change since the previous release".
3. Tag the merge commit: `git tag -a smo-vX.Y.Z -m "SMO X.Y.Z" <sha> && git push origin smo-vX.Y.Z`. Tags are never moved or deleted; a bad release is superseded by the next PATCH.
4. Right after the tag, empty `smo/scripts/breaking_change_waivers.json` (`{}`) in a PR of its own: the new tag is the next comparison's base, and every waiver now matches nothing, so the job fails as stale on every PR until the file is empty.
5. The tag is the release. Pushing it starts `.github/workflows/release-images.yml` (below). In the same release PR as the changelog, update the supported-versions table in `SECURITY.md`: the newest `0.MINOR` line is supported and the previous one is dropped (a test checks that every line listed has a release).

A person cuts the tag. It is not something an automated change does on its own.

Before merging the release PR every check must be finished and green, CodeQL included: its jobs are the only gate on the sign-in and signing code. CodeQL is GitHub's default code-scanning setup, configured in the repository's settings rather than by a workflow in `.github/workflows/` (on a pull request its checks are "CodeQL" and one "Analyze (<language>)" per language), and the ruleset on `main` has a code-scanning rule that refuses a merge while CodeQL reports a high or critical security alert. If the CodeQL jobs of the release PR sit queued and the Actions page offers no re-run (this happened for 0.6.0: queued for two hours, the run marked failed, re-run refused), push a real commit to the release branch (a documentation fix, or a merge of `main`) so that a new scan starts; do not push an empty commit or close and reopen the PR, and do not merge without CodeQL.

## Publishing images, signatures and notes

`release-images.yml` runs on a pushed `smo-v*` tag, or by hand (`workflow_dispatch`, input `tag`) for a tag that already exists. It
builds every image the compose file builds (`python scripts/release_images.py names`), pushes
`ghcr.io/<owner>/<repo>/smo-<module>:<version>`, signs each by digest with cosign (keyless: the signature names this workflow
as the signer), attaches BuildKit provenance (SLSA, `mode=max`) and an SBOM beside the image, and writes the release notes
(`scripts/release_notes.py`: the `CHANGELOG.md` section plus the merged PR titles since the previous tag) onto the tag's GitHub release.
Each image's `name@sha256:...` is in the run's summary.

Verify an image before running it (replace the module, version and repository):

    cosign verify ghcr.io/<owner>/<repo>/smo-ran-nf-oam:0.1.0 \
      --certificate-identity-regexp '^https://github.com/<owner>/<repo>/\.github/workflows/release-images\.yml@refs/' \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com
    docker buildx imagetools inspect ghcr.io/<owner>/<repo>/smo-ran-nf-oam:0.1.0 --format '{{ json .Provenance }}'

### The repository was renamed (October 2026)

The repository `ai-ran-ref` became `ai-ran-smo`. Images are named after the repository, and a container registry does not redirect, so:

- Releases up to and including `smo-v0.4.0` stay at `ghcr.io/rsys-bshankar/ai-ran-ref/smo-<module>:<version>`; verify them with `<repo>` = `ai-ran-ref` in the command above (their signatures name that repository's workflow).
- Releases after it are published at `ghcr.io/rsys-bshankar/ai-ran-smo/smo-<module>:<version>`; verify them with `<repo>` = `ai-ran-smo`.
- The Helm chart's default `image.registry` follows the new name, so a chart at a commit after the rename needs images of a release published after it; to run `0.4.0` from the chart, set `image.registry=ghcr.io/rsys-bshankar/ai-ran-ref`.
- Web, `git` and API URLs of the old name redirect (until a repository of that name is created again); update remotes with `git remote set-url origin https://github.com/rsys-bshankar/ai-ran-smo`.
- A package published by the release workflow is private until someone makes it public (package settings of each of the images listed by `python scripts/release_images.py names`).

## The first tag

`smo-v0.1.0` is the point at which the production-readiness slices recorded under `CHANGELOG.md`'s first section were in `main`. It is a source release: images are not built or published by tag until `OPS-4.2`, so a consumer builds them from the tag with `docker compose up -d --build`.
