### PR-CONV-S1.8 — R1 Termination routes ProvMnS by class through the MnS registry; milestone M1

**What was built.** `r1-termination/app/mns_routing.py`: a request to `/ProvMnS/{version}/{LDN}` goes, after the gateway's usual checks (token,
budget, path, role policy, kill switch), to `<mnsAddress>/<LDN>` of the producer the MnS registry names for the class of the LDN's last RDN
(`producer_for`: a PROV_MNS MnsInfo of that version whose MgmtDataInfo lists the class and whose mnsScope holds the DN, the closest scope
first, an unscoped one last, then by DN). The registry's own classes go to SME (`SME_URL`) without a lookup: that is how a module registers before
anything is known of it. There is no ProvMnS entry in `ROUTES`, and `tests/test_mns_routing.py` fails on one. `/bootstrap` also names SO SMOS
and AIMgF (`so-smos`, `aimgf`; TE&IV joins with S3.1). The introspection cache is unchanged (no Valkey: no load test has shown it short).

**The registry copy.** Read from SME directly, as `internal`, every page, and kept for `R1_MNS_REGISTRY_CACHE_SECONDS` (default 10). A class not
found in a copy over a second old reads it again; a 2xx write of a registry class through the gateway (every registration) drops the copy, so a
module that just registered is reached on its next request (found by the M1 test: the copy read for the first, refused request was still
fresh when Echo registered). SME unreadable: the last copy is used; none, 503 `MNS_REGISTRY_UNAVAILABLE` with `Retry-After`. Not found: 404
`NO_ROUTE` naming the class, version and DN. The role policy needed no change: an rApp may read under `/ProvMnS` and change nothing.

**Milestone M1, its exit test.** `tests_integration/test_m1_echo_through_r1.py` runs SME, R1 Termination and Echo in one process, with the network
replaced by host name (the modules' synchronous calls to the apps' TestClients, R1's asynchronous ones through `httpx.ASGITransport`), so every
hop is the deployed one: Echo, with no route in R1's table, starts and registers itself through R1 with a token from SME's real token flow; then
createMOI, getMOIAttributes (subtree), modifyMOIAttributes (merge patch) and deleteMOI of SubNetwork and ManagedElement through R1 by class
name reach it, and a registry read through R1 lists Echo (SubNetwork, ManagedElement, at `http://echo:8000/ProvMnS/19.2.0`) and SME. It passes.

**Found on the way.** The interface gate's base-ref read (`scripts/baseline_dirs.read_at`, PARALLEL-SAFE) ran `git ls-tree` from `smo/`, which
lists only entries under that prefix, so against a base ref with the per-module directory it read an empty baseline and reported every entry
as growth (910 problems on this branch against `main`). It passes `--full-tree` now, and `tests_integration/test_interface_gate.py` reads from a
subdirectory.

**Not done.** R1Client finding peers by CAPIF discovery instead of `ROUTES` (S1.16); a load test of the routing (S8).
