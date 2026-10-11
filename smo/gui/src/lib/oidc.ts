/**
 * OIDC sign-in (PR-SEC-6): what the sign-in page shows for the reason code the backend puts on /login?oidc_error=<code>, and where the single-sign-on button points.
 * The codes are the backend's (gui-bff/app/oidc.py, REASONS); the page never shows text from the identity provider, only its own wording for a code it knows.
 * Used by the sign-in page (`pages/login/sections/SignInForm.tsx`, `pages/login/data/queries.ts`); covered by `oidc.test.ts`.
 * The wording comes from the catalog (`i18n/en/lib.ts`, `oidc.*`, GUI-6.4).
 */

import type { TKey, Translator } from "../i18n";
import { ENGLISH } from "./domain";

/**
 * The sign-in options the BFF reports before login (GET /api/auth/config): whether the password form is offered, the login mode (PR-SEC-7.6: "oidc" closes the
 * password form to every account but a break-glass one, `breakGlass` says one can still be used) and the OIDC provider with the path that starts the sign-in.
 */
export interface AuthConfig {
  localLogin: boolean;
  // PR-SEC-7.6: "oidc" closes the password form to every account but a break-glass one (`breakGlass` says one can still be used)
  loginMode?: "both" | "oidc" | "local";
  breakGlass?: boolean;
  oidc: { enabled: boolean; providerName?: string; loginUrl?: string };
}

const MESSAGES: Record<string, TKey> = {
  idp_unavailable: "oidc.error.idp_unavailable",
  idp_error: "oidc.error.idp_error",
  access_denied: "oidc.error.access_denied",
  invalid_state: "oidc.error.invalid_state",
  token_exchange_failed: "oidc.error.token_exchange_failed",
  token_invalid: "oidc.error.token_invalid",
  no_role: "oidc.error.no_role",
  account_disabled: "oidc.error.account_disabled",
  invalid_scope: "oidc.error.invalid_scope",
  too_many_logins: "oidc.error.too_many_logins",
};

/** The message for a reason code, in the language of `t` (English by default); null for no code; a generic line for a code this build does not know. */
export function oidcErrorMessage(code: string | null | undefined, t: Translator = ENGLISH): string | null {
  if (!code) return null;
  return t(Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : "oidc.error.unknown");
}

/** Where the browser goes to start a sign-in: only a same-origin path the backend named, never a URL from elsewhere. */
export function oidcLoginHref(cfg: AuthConfig | undefined): string | null {
  const url = cfg?.oidc.enabled ? cfg.oidc.loginUrl : undefined;
  return url && url.startsWith("/api/") ? url : null;
}
