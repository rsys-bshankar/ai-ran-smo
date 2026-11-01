### PR-CONV-S1.14 — Access control per TS 28.319: Identity, Role and AccessRule as ProvMnS classes in SME; every ProvMnS route's scope check reads AccessRules

**What was built.**
- `smo_shared/auth/`. `AccessRuleAuthorizer` is now the default decision of the router's scope step (`mns/checks.py` `default_authorizer`), so every
  ProvMnS route of every module reads AccessRules. It requires three things: the caller's ADR 0005 claim on the target's placement (region, tenant,
  site cluster: properties of the target, not of its DN); the claim's SubNetwork axis (D15) expressed as AccessRules (`claim_rules`: ALLOW under each
  SubNetwork named, wherever it sits); and the AccessRules of the caller's TS 28.319 Identity through its Roles (`rules_by_identity`), when it has
  one. A caller with no Identity is not managed by them, so nothing changes until an operator writes one. With no Identity source the decision is
  ADR 0005's, proved equal for every combination of seven claims, seven DNs and three placements and the four operations
  (`shared/tests/test_auth_access.py`).
- The five D15 roles (`auth/roles.py`): PLATFORM_ADMIN (everything), NOC_OPERATOR (read everything, write outside `SubNetwork=SMO`, the platform's
  own), ML_ENGINEER (read; write `ML*` and `AIML*`), RAPP_DEVELOPER (read; write `Rapp*`), READ_ONLY_AUDITOR (read); every role may manage
  subscriptions (notification access is read access).
- SME serves Identity, Role and AccessRule (`sme/app/msac_store.py`) from the vendored `TS28319_MsacNrm.yaml`, under `SubNetwork=SMO` (the NRM's
  OpenAPI declares no containment; `ClassRegistry.contain` adds it for SME's own class registry). The five roles and their rules are written the first
  time SME finds them missing; a D15 role is not deleted; an Identity carries no `credential` (SME keeps no password); an AccessRule this build cannot
  evaluate is refused. SME's own routes decide with these rows, read in the request's session (`checks.SESSION`, set by the service around each
  operation), so a NOC_OPERATOR cannot change the rules that bind it. SME now registers these classes, the subscriptions, and SubNetwork for reads of
  its container, within mnsScope `SubNetwork=SMO`, so R1 Termination routes `SubNetwork=SMO` to SME and every other SubNetwork to the network's producers.
- Another module reads SME's rules through R1 with `SMO_ACCESS_RULES=sme` (`SmeAccessRules`, kept `SMO_ACCESS_RULES_CACHE_SECONDS`; SME unreadable with
  no copy: fail closed).

**Notification access.** The same decision for a read: a subscription delivers a notification about an instance only when its subscriber may read
it (`history.d/PR-CONV-S1.13.md`).

**MSAC tests still pass.** Echo's MSAC step (`ECHO_MSAC_RULES`, S1.5) is unchanged and its suite passes; RAN NF OAM's own MSAC tables are untouched.

**Proved.** `tests_integration/test_subscriptions_and_access_rules.py` through R1: with `SMO_ACCESS_RULES=sme` Echo's routes decide with the
Identities SME keeps (a READ_ONLY_AUDITOR reads and may not write, a RAPP_DEVELOPER may not write a ManagedElement, a NOC_OPERATOR may, someone with
no Identity is not managed); a person scoped to SubNetwork S2 may not subscribe under S1 and is told only of S2's changes.
`sme/tests/test_msac_store.py` (the roles, the vetoes, SME's own decisions, a rule change applying at once, the claim's SubNetwork axis).

**Not done.** "Every route" is every ProvMnS route: the modules' remaining own routes keep ADR 0005's helpers until they move onto the router. The
GUI's admin views of these classes are S6.11, the BFF's role tiers S6.6.
