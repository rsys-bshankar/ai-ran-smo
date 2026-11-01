"""`smo_shared.auth`: access control per TS 28.319 (plan task S1.14).

  access.AccessRuleAuthorizer   the decision of the ProvMnS router's scope step (`mns.checks.ScopeStep`), from AccessRules: the caller's ADR 0005 claim
                                expressed as AccessRules (its SubNetwork axis as selectors; region, tenant and site cluster on the target's placement), and
                                the AccessRules of the caller's TS 28.319 Identity through its Roles
  access.rules_by_identity      Identity, Role and AccessRule resources (a ProvMnS read) as each identity's rules
  access.SmeAccessRules         those rules read from SME through R1 Termination, kept a few seconds
  roles                         the five D15 roles (PLATFORM_ADMIN, NOC_OPERATOR, ML_ENGINEER, RAPP_DEVELOPER, READ_ONLY_AUDITOR) and their AccessRules
"""

from .access import AccessRuleAuthorizer, SmeAccessRules, claim_rules, rules_by_identity
from .roles import D15_ROLES, role_rules

__all__ = ["D15_ROLES", "AccessRuleAuthorizer", "SmeAccessRules", "claim_rules", "role_rules", "rules_by_identity"]
