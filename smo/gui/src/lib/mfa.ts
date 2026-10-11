/**
 * One-time codes (PR-SEC-7): the parts of the sign-in's second step and of the Account security page that are not rendering: the challenge types, the code format checks,
 * and the wording for each failure.
 * The reason codes are the backend's (gui-bff/app/main.py); the page shows its own wording for each and never the server's text for an unknown one.
 * Used by `auth/AuthContext.tsx`, `main.tsx`, `components/LoginCodeStep.tsx`, `components/TotpEnrolment.tsx` and the login, security and admin pages under `pages/`; covered by `mfa.test.ts`.
 */

import type { Me } from "../api/types";
import { translate, type Params, type TKey } from "../i18n";

/** A message in English: what the helpers below answer in when no translator is passed. */
const english = (key: TKey, params?: Params) => translate("en", key, params);

/** What `POST /api/login` answers for an account with a one-time code: no session yet, a challenge to send back with the code. */
export interface LoginChallenge { mfaRequired: true; challenge: string; expiresIn: number }
/** The session `POST /api/login` or `POST /api/login/totp` opens; `recoveryCodesLeft` is there when a recovery code was spent. */
export type SignedIn = Me & { recoveryCodesLeft?: number };

/**
 * Narrows the answer of POST /api/login: true when it is a second-step challenge (no session yet) rather than a signed-in user.
 */
export function isChallenge(result: unknown): result is LoginChallenge {
  return typeof result === "object" && result !== null && (result as { mfaRequired?: unknown }).mfaRequired === true
    && typeof (result as { challenge?: unknown }).challenge === "string";
}

/** The secret in groups of four, for typing into an authenticator app by hand. */
export function groupSecret(secret: string): string {
  return secret.replace(/\s+/g, "").replace(/(.{4})(?=.)/g, "$1 ");
}

/** What was typed, without the spaces and dashes people add when reading a code aloud. */
export function cleanCode(input: string): string {
  return input.trim().replace(/[\s-]+/g, "");
}

/** A six-digit one-time code (as against a recovery code). */
export function isTotpCode(input: string): boolean {
  return /^\d{6}$/.test(cleanCode(input));
}

/** A recovery code is sixteen letters and digits (shown in four groups of four). */
export function isRecoveryCode(input: string): boolean {
  return /^[a-z0-9]{16}$/i.test(cleanCode(input));
}

/**
 * True for what the second step accepts: a six-digit code or a sixteen-character recovery code. The server decides whether it is right; this only avoids sending an obviously malformed one.
 */
export function isPlausibleCode(input: string): boolean {
  return isTotpCode(input) || isRecoveryCode(input);
}

/** The recovery codes as text, one per line with a final newline, for the clipboard. */
export function recoveryCodesText(codes: string[]): string {
  return codes.join("\n") + "\n";
}

/** The sentence about how many recovery codes remain, in the language of `t` (`useT()`; English by default); at zero it tells the user to
 * generate new ones. */
export function recoveryLeftText(left: number, t: (key: TKey, params?: Params) => string = english): string {
  if (left <= 0) return t("mfa.recoveryLeft.none");
  return t("mfa.recoveryLeft", { count: left });
}

const LOGIN_MESSAGES: Record<string, string> = {
  INVALID_CODE: "That code is wrong, or has been used already. Wait for the next one on your device and try again.",
  CHALLENGE_INVALID: "The sign-in took too long. Enter your password again.",
  TOO_MANY_ATTEMPTS: "Too many failed attempts — try again in a few minutes.",
  INVALID_CREDENTIALS: "Invalid username or password.",
  LOGIN_MODE_OIDC_ONLY: "Local sign-in is closed on this console. Use the single sign-on button.",
  BREAK_GLASS_NEEDS_TOTP: "This break-glass account has no one-time code set up, so it cannot sign in. Ask another admin.",
  TOTP_KEY_UNAVAILABLE: "The one-time code cannot be checked on this server. Ask an administrator.",
  LOCAL_LOGIN_DISABLED: "Local sign-in is turned off. Use the single sign-on button.",
};

/** The sentence for a failed sign-in step, from the backend's reason code (its `title`); the status stands in for an old backend that sends none. */
export function loginErrorMessage(err: { status?: number; title?: string; message?: string }): string {
  if (err.title && Object.prototype.hasOwnProperty.call(LOGIN_MESSAGES, err.title)) return LOGIN_MESSAGES[err.title];
  if (err.status === 429) return LOGIN_MESSAGES.TOO_MANY_ATTEMPTS;
  if (err.status === 401) return LOGIN_MESSAGES.INVALID_CREDENTIALS;
  return err.message || "Sign-in failed.";
}

/** True when the user must enrol a one-time code first (the backend's GUI_ADMIN_MFA_REQUIRED) and is not already on the Account security page, the one route open to them; the router then redirects there. */
export function mustEnrol(me: Pick<Me, "mfaEnrolmentRequired"> | null | undefined, pathname: string): boolean {
  return Boolean(me?.mfaEnrolmentRequired) && pathname !== "/security";
}

/** What the Users table says about a user's one-time code. */
export function mfaLabel(u: { username: string; totpEnrolled?: boolean }): string {
  if (u.username.startsWith("oidc:")) return "identity provider";
  return u.totpEnrolled ? "enrolled" : "not set up";
}
