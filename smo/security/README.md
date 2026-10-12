# Dynamic security scans (PR-V-7)

`.github/workflows/smo-dast.yml` starts the stack behind the TLS edge every night (and on a pull request that changes the workflow, this directory or
`scripts/dast_baseline.py`) and runs four scans. It is not a merge gate: a run that finds something new fails, and the nightly failure is the signal.

| Scanner | Target | What it looks for | Baseline of accepted findings |
|---|---|---|---|
| OWASP ZAP API scan | the gateway (`r1-termination`), driven by `docs/openapi/r1-termination.json` | what an unauthenticated caller can reach: missing headers, error leakage, injection on the documented parameters | `zap-baseline.json` |
| testssl.sh | the TLS edge, `localhost:8443` (gateway) and `:3443` (GUI) | protocol versions, ciphers, certificate, known TLS vulnerabilities | `testssl-baseline.json` |
| OWASP ZAP API scan, with a token (job `dast-authenticated`, two runs: rApp and SMO module) | each module's own document (`docs/openapi/<module>.json`, 15 modules, in three groups), its paths moved under the gateway's prefix for it (`scripts/dast_gateway_spec.py`), called through the gateway with a token from `scripts/dast_token.py`: an rApp (an invoker without the enrollment secret) or an SMO module (one with it) | what a caller past the gateway's authentication reaches in the modules: error handling, injection on the parameters, and, for the module identity, the routes an rApp may not use | `zap-baseline.json` (the same file) |
| Nuclei, default templates, severity low and above | what is reachable from outside: the TLS edge of the gateway (`:8443`) and of the GUI (`:3443`) | known misconfigurations and exposures (e.g. that R1's `/metrics`, which is for the internal scraper, is not served by the edge) | `nuclei-baseline.json` |

The three tool images are pinned by digest in the workflow (`ZAP_IMAGE`, `TESTSSL_IMAGE`, `NUCLEI_IMAGE`; pinned 2026-10-05: ZAP `stable`, testssl.sh `3.2`,
Nuclei `v3.9.0`). Nuclei fetches its templates when it starts, so a run can see templates newer than the last one; that is the point of a nightly scan, and
the cost is a finding that appears without a change of ours. Every report (`zap.html`, `zap.json`, the testssl.sh output, `nuclei.jsonl`) is uploaded as the
artifact `dast-reports` for 30 days.

## When a run fails

1. Open the job's log: each tool prints `NEW <key> (<detail>)` for each finding the baseline does not accept. The report in the artifact has the request and the response.
2. Decide: **fix it** (the normal answer), or **accept it** with a reason a reviewer can judge, by adding `"<key>": "reason"` to the tool's baseline in a pull request. A key may use
   shell wildcards. A reason that says only "false positive" is not one; say why it cannot be exploited here, or what makes it a deliberate choice.
3. A baseline entry that matches nothing is printed as `STALE` and does not fail the run; remove it once it stays stale.

The scripts take the same inputs, so a report can be checked on a laptop: `scripts/dast_baseline.py zap|testssl|nuclei <baseline> <report>`.

## What is not scanned yet

- **Authenticated calls: done** (above), through the gateway, as two identities. The gateway's own document has a single route for everything it proxies, so a token on that scan reaches nothing behind it (the first version of this scan did exactly that: 7 URLs, the same as without a token); the module documents are what reach the modules. The scan sends every documented method with the token, so it changes data in its throwaway stack; the token-abuse suite (`tests_integration/test_token_abuse.py`, PR-V-7b) still covers identity and path handling.
- **Each module on the internal network.** Only the gateway and the GUI are reachable from outside, so only they are scanned. A scan of every module's own specification would find what
  a compromised neighbour could reach.
- **The southbound side** (NETCONF/SSH to a managed element) and the Helm deployment's own surface.

## The external penetration test

An external test is a criterion for 1.0.0, commissioned after the security hardening items and against the 0.5.0 release candidate or later (`docs/VALIDATION.md`, V-7c). What it
should be given is in `docs/PENTEST_SCOPE.md`.

**The token scan runs without the gateway's rate limiter.** `dast-authenticated` adds `docker-compose.load.yml` to `COMPOSE_FILE` (`R1_RATE_PER_SECOND=0`): ZAP's active scan sends thousands of requests from one caller, and a limiter that answers some of a true/false payload pair with 429 and the rest with 422 looks to ZAP's boolean-based rule (40018) like an injection. The limiter is tested by the stress lane and the unit and integration suites, not by this scan. If a `SQL Injection [40018]` alert appears, read the "gateway requests by status" line of the job: 429 in it means the limiter is back on.
