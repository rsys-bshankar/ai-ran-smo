---
section: Added
---
- **Subscriptions and access rules over ProvMnS (plan tasks S1.13, S1.14).** Every ProvMnS producer (Echo, SME) now serves TS 28.622
  `NtfSubscriptionControl` (recipient, notification types, scope, filter) and `HeartbeatControl` (`heartbeatNtfPeriod`, `triggerHeartbeatNtf`):
  create one under a SubNetwork or ManagedElement and the producer's notifications are delivered to its recipient through the notification outbox
  (SSRF-guarded; a loopback or metadata recipient is refused), only for the instances the subscriber may read. SME serves TS 28.319 `Identity`, `Role`
  and `AccessRule` under `SubNetwork=SMO`, with the five roles PLATFORM_ADMIN, NOC_OPERATOR, ML_ENGINEER, RAPP_DEVELOPER and READ_ONLY_AUDITOR written
  for you. Every ProvMnS route now decides from AccessRules: your ADR 0005 claims behave exactly as before, and an Identity you create for a console
  user (`smo-gui:<user>`) or an invoker narrows what it may do. Another module reads SME's rules when `SMO_ACCESS_RULES=sme`
  (`SMO_ACCESS_RULES_CACHE_SECONDS`, default 10). No schema revision; Echo's database role gets `notification_outbox`.
