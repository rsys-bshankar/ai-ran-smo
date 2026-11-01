---
section: Security
---
- **TE&IV's WG10 door refuses a decorator value smo-teiv 0.3.0 would write into its SQL unescaped.** smo-teiv's updateDecorator pastes the value into the SQL text, so a `'` in it is an SQL injection (ADR 0013 section 12.5, d2; to be reported upstream with plan task S3.25). TE&IV's `/topology-inventory/v1/manage-decorators` answers 400 `INVALID_DECORATOR_VALUE` for a decorator name or value holding `'`, `"` or `\`, before smo-teiv is called; TE&IV's own decorator values never hold them. Keep smo-teiv's API unreachable except through TE&IV (D19: the internal network `teiv`, the chart's NetworkPolicies).
