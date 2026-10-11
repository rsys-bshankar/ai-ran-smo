/**
 * OIDC sign-in (PR-SEC-6): what the sign-in page shows for the reason code the backend puts on /login?oidc_error=<code>, and where the single-sign-on button points.
 * The codes are the backend's (gui-bff/app/oidc.py, REASONS); the page never shows text from the identity provider, only its own wording for a code it knows.
 * Used by the sign-in page (`pages/login/sections/SignInForm.tsx`, `pages/login/data/queries.ts`); covered by `oidc.test.ts`.
 */

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

const MESSAGES: Record<string, string> = {
  idp_unavailable: "The identity provider could not be reached. Try again in a moment.",
  idp_error: "The identity provider reported an error. Try again, or ask an administrator.",
  access_denied: "The identity provider did not grant the sign-in.",
  invalid_state: "The sign-in did not start in this browser, or it took too long. Start it again.",
  token_exchange_failed: "The identity provider refused the sign-in. Try again, or ask an administrator.",
  token_invalid: "The identity provider's answer could not be verified. Ask an administrator.",
  no_role: "You signed in, but none of your groups gives access to this console. Ask an administrator.",
  account_disabled: "Your account is disabled. Ask an administrator.",
  invalid_scope: "Your account's region or tenant scope from the identity provider is not valid, so access was refused. Ask an administrator.",
  too_many_logins: "Too many sign-ins are in progress. Try again in a minute.",
};

/** The message for a reason code; null for no code; a generic line for a code this build does not know. */
export function oidcErrorMessage(code: string | null | undefined): string | null {
  if (!code) return null;
  return Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : "The single sign-on attempt failed.";
}

/** Where the browser goes to start a sign-in: only a same-origin path the backend named, never a URL from elsewhere. */
export function oidcLoginHref(cfg: AuthConfig | undefined): string | null {
  const url = cfg?.oidc.enabled ? cfg.oidc.loginUrl : undefined;
  return url && url.startsWith("/api/") ? url : null;
}
