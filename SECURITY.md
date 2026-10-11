# Security Policy

## Scope and intended use

This repository is a **reference implementation** of an O-RAN SMO platform
(`smo/`) together with the 3GPP and O-RAN specifications it is grounded on
(`specs/`). It is meant for learning, integration testing and demos. It has not
been hardened or audited for production, and it has never been exposed to an
untrusted network. Do not deploy it as-is on the public internet or in front of
a live RAN.

## Supported versions

Releases are tagged `smo-vX.Y.Z` (`smo/docs/RELEASES.md`). Until `1.0.0`, only the newest `0.MINOR` line and the tip of the
default branch (`main`) receive security fixes, which land in `main` first and then in a new PATCH release of that line.
Older lines and forks receive no backports.

| Version | Supported |
| ------- | --------- |
| `main` (latest commit) | :white_check_mark: |
| `0.8.x` | :white_check_mark: |
| Anything else | :x: |

## Reporting a vulnerability

**Please do not open a public issue or pull request for a security problem.**

Report it privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability** (GitHub private vulnerability reporting), or go
straight to
<https://github.com/rsys-bshankar/ai-ran-smo/security/advisories/new>.
If that option is not available to you, open an issue that says only that you
have a security report to make, with no details, and a maintainer will arrange
a private channel.

Please include:

- what you found and which module or file it is in (for example `r1-termination`,
  `gui-bff`, `sme`);
- the steps or request that reproduce it, and the commit you tested;
- the impact as you see it.

What to expect (best-effort targets, since this is a maintained reference
project and not a product with an SLA):

| Step | Target |
|---|---|
| Acknowledgement of your report | within 5 working days |
| First assessment (accepted, declined, or need more information) | within 14 days |
| Fix or mitigation for an accepted report | depends on severity; you are kept informed |

If the report is accepted, we fix it on `main`, credit you in the advisory
unless you prefer otherwise, and publish a GitHub Security Advisory once the fix
is available. If it is declined (for example because it is a documented
limitation below, or is not reproducible), we say why.

## Security model of the SMO reference implementation

What is in place, so you can judge what counts as a vulnerability:

- **R1 gateway authentication.** Every call from an rApp goes through R1
  Termination, which introspects the bearer token against SME's OAuth2 issuer
  (RFC 7662) before proxying. Only `/health` and `/bootstrap` are exempt at the
  gateway; tokens are issued by SME directly.
  See [`smo/docs/ARCHITECTURE.md`](smo/docs/ARCHITECTURE.md#r1-api-conventions).
- **Why `/bootstrap` is unauthenticated, and what it reveals.** An rApp calls
  `GET /bootstrap` to find SME's token endpoint *before* it has a token, so it
  cannot require one. It takes no input and reads no data; the answer is two
  entries (`service-apis`, `published-apis`) naming SME's address on the
  container network (or the gateway's public base URL) and the OAuth2 token
  endpoint, nothing else: no identity, secret, token or rApp data, and each
  endpoint it names still checks credentials. Leaving it open discloses an
  internal hostname and API paths and gives a free probe of a live gateway; a
  report that only says "`/bootstrap` needs no token" is this documented
  behaviour. Narrow who can ask, if the deployment can enforce it: the Helm
  chart's `bootstrapNetworkPolicy` (limits who reaches the gateway pods; a
  NetworkPolicy cannot select a path), `ingress.r1.bootstrapAllowedSourceRanges`
  (an ingress-nginx rule for the exact path), and `R1_BOOTSTRAP_KEY[_FILE]`, an
  optional shared key sent as `X-Bootstrap-Key` (constant-time compare, 401
  otherwise; off by default). The key is one secret every rApp holds, a gate
  against scanners, not an identity. Details: `smo/r1-termination/README.md`.
- **Rate limiting.** R1 Termination gives each invoker a token bucket (429
  with `Retry-After`). By default each gateway replica counts for itself;
  `R1_RATE_STORE=postgres` shares one budget across replicas and **fails
  open** if the database errors (the limiter is a fairness control, and the
  token check does not depend on it; the failure is logged and counted in
  `smo_rate_store_errors_total`).
- **Network exposure.** In `docker-compose.yml` only R1 Termination (`:8080`),
  the GUI (`:3000`) and Postgres (`:5432`) publish host ports.
  The optional `tls` profile adds an nginx edge on `:3443` (GUI) and `:8443` (R1)
  with TLS 1.2+ and HSTS; the plain ports stay open until a deployment removes them.
  Services behind the edge speak HTTP on the compose network, unless mutual TLS between services is switched on
  (`SMO_MTLS=on`, `docker-compose.mtls.yml` or the chart's `mtls.enabled`; off by default, `docs/ARCHITECTURE.md`, "Mutual TLS between
  services"). With it every service requires a client certificate from one CA and every module's calls present one. The development CA of
  `scripts/mtls_certs.py` is for trials: its key must stay off the hosts that run the stack, and a deployment brings its own CA. Not covered: the
  certificate name is not used for identity (the token check at the gateway is unchanged), Postgres has no TLS (`PR-SEC-2.4`), `mock-o1-adaptor`,
  the GUI's nginx and the GUI backend's own port stay plain HTTP, and a server loads its certificate at start (renewal needs a restart).
- **Operator GUI.** Session JWT in an `HttpOnly; Secure; SameSite=Strict`
  cookie, CSRF double-submit on unsafe methods, role checks (viewer / operator /
  admin) re-read on every request, account lockout after repeated failures, an
  append-only audit log (append-only in the application; not hash-chained, `STD-4.7`), a strict CSP, and a proxy that never forwards browser
  credentials to R1. Optional OIDC sign-in (`GUI_OIDC_ENABLED`, off by default): authorization code with PKCE, the ID token validated
  against the provider's JWKS (signature, issuer, audience, expiry, nonce; asymmetric algorithms only), the state held in the database and tied to the
  browser by a cookie, roles from a configured group map with no role as the default (refused), and the same session as a password login; the
  provider enforces multi-factor, and the local admin stays as the break-glass account (`GUI_LOCAL_LOGIN_ENABLED=false` removes it).
  Details: [`smo/gui/README.md`](smo/gui/README.md#security), [`smo/gui-bff/README.md`](smo/gui-bff/README.md) section 2.9.
- **Outbound callbacks.** Any caller-supplied callback URL is called through
  one helper (`smo_shared.webhook`); RAN NF OAM vendor discovery never fetches a
  URL taken from a request body.
- **Platform audit.** Every authenticated change through R1 Termination is recorded in a hash-chained,
  numbered audit table (`python -m smo_shared.audit verify` and `export`); the request body is never recorded.
- **Secrets.** The GUI admin password is generated at first start when
  `GUI_ADMIN_PASSWORD` is unset and written to a `0600` file; `GUI_JWT_SECRET`,
  when unset, is generated once and stored in the GUI database so every instance
  shares it. The Postgres password is generated by `smo/scripts/init_secrets.sh`
  into `smo/secrets/` (not committed) and read through `*_FILE`; every secret is
  inventoried in [`smo/docs/SECRETS.md`](smo/docs/SECRETS.md).

## Known limitations (not vulnerabilities)

These are deliberate Phase 1 scope cuts. Reports that only restate them are
declined, but a way to exploit one in a surprising way is welcome.

- The compose file publishes Postgres on `:5432` (its password is generated per
  checkout by `scripts/init_secrets.sh`, but the port is still reachable from the
  host's network). Unpublish the port for anything beyond a laptop demo.
- **No TLS** is terminated inside the stack except by the optional `tls` profile's
  edge. The GUI cookie is marked `Secure`, so put a TLS-terminating proxy (or that
  edge) in front of it; R1 Termination at `:8080` is plain HTTP.
- Module-to-module traffic on the compose network is unauthenticated apart from
  the R1 token check at the gateway; there is no mTLS between services.
- OAuth2 tokens are opaque and introspected. SME checks a token's `scope`
  against the published APIs when it issues the token, but R1 Termination
  checks only that a token is active, not what its scope allows. An invoker
  onboarded with a PEM key can authenticate with an RFC 7523 signed client
  assertion; SMO's own modules authenticate with their onboarding secret
  (`OI-2-oauth2-scope`, `SA-SME-1-public-key` in
  [`smo/HISTORY.md`](smo/HISTORY.md)).
- Package signatures are optional and off by default. With
  `ONBOARDING_TRUST_STORE` unset Onboarding checks no signature (its
  `signatureVerified` means "validated"); with it set, a signed package must
  verify against the operator's publisher keys (ed25519), and
  `ONBOARDING_REQUIRE_SIGNED_PACKAGES` refuses an unsigned one. The trust
  anchor is a list of keys in a file: no certificate chain, expiry, revocation
  list or timestamp, and the check is at fetch time only. The sample packages
  are signed with a demo key whose private half is committed under
  `smo/samples/demo-signing/`: never trust it in production
  (`smo/docs/RAPP_PACKAGING.md` section 8).
- The O1 transports are not hardened: RAN NF OAM speaks RFC 6241-shaped
  NETCONF `edit-config` and RFC 8040 RESTCONF over plain HTTP, without TLS or
  authentication, to the adaptor, and the stack has not been run against a real
  RAN.
- OIDC login: one provider, no LDAP, no back-channel logout, no native MFA (the provider's), and the ID token is not kept, so the end-session
  request has no `id_token_hint` and the provider may ask the person to confirm. A user's role is set from the token at each sign-in, so a role an admin sets by hand on an
  OIDC user lasts until that user's next sign-in. Tested against a fake provider in unit tests and against Keycloak in CI, not against any other provider.
- Backend RBAC exists only in `gui-bff`; R1 service APIs authenticate callers
  but do not authorise per resource.
- Personal data (GUI users and the names written next to operator actions) is inventoried in
  [`smo/docs/PRIVACY.md`](smo/docs/PRIVACY.md). Deleting a GUI user removes the account, its
  sessions and its failed-login counter; the audit rows and module records that name the user
  stay (the same document says why and what the options are). The GUI's own database is not
  covered by `scripts/db_backup.sh`, which dumps Postgres; `scripts/dr_backup.sh` copies it with the rest to an
  S3-compatible bucket (compose). Backups hold every table including credentials in plaintext, are not
  encrypted by the scripts, and are as sensitive as the database: [`smo/docs/DISASTER_RECOVERY.md`](smo/docs/DISASTER_RECOVERY.md)
  has the targets, the bucket guidance and what is not covered.

The authoritative, current list of open items is
[`smo/OPEN_ITEMS.md`](smo/OPEN_ITEMS.md). How what exists maps to ISO/IEC 27001:2022 Annex A
and the NESAS/SCAS test categories, with the evidence and the gaps, is
[`smo/docs/CONTROL_MATRIX.md`](smo/docs/CONTROL_MATRIX.md); where data lives and what leaves a
site is [`smo/docs/DATA_RESIDENCY.md`](smo/docs/DATA_RESIDENCY.md); the scope drafted for an
external penetration test is [`smo/docs/PENTEST_SCOPE.md`](smo/docs/PENTEST_SCOPE.md).

## Dependencies

The Python services install their dependencies from a hashed lock (`smo/requirements/*.txt`,
`pip install --require-hashes`, `smo/Dockerfile`); the GUI is locked by `smo/gui/package-lock.json`;
base images are pinned by digest. Dependabot is configured (`.github/dependabot.yml`: GitHub
Actions, npm, pip, Docker, weekly), pull requests are checked by dependency review, and the
released images are scanned (Trivy), signed (cosign, keyless) and published with provenance and
an SBOM (`.github/workflows/`). Whether GitHub code scanning (CodeQL) and secret scanning are
enabled is a repository setting that this repository does not record.

## Specifications in `specs/`

The files under `specs/` are reference copies of third-party standards material
(3GPP, O-RAN); the release of each is recorded in [`specs/README.md`](specs/README.md#specification-release-table-std-21). Report issues in their content to the originating standards body, not
here.
