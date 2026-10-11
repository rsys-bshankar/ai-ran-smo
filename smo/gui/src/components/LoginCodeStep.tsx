/**
 * The second step of the sign-in form (PR-SEC-7.2): shown after the password was accepted for an account that has a one-time code. Used by `pages/login/sections/SignInForm.tsx`;
 * the code rules (six digits or a recovery code) are in `lib/mfa.ts`.
 */

import { useState, type FormEvent } from "react";

import { useT } from "../i18n";
import { cleanCode, isPlausibleCode } from "../lib/mfa";

/** The second step of a sign-in (PR-SEC-7.2): the six-digit code from the authenticator app, or one recovery code. The password was right already. */
export function LoginCodeStep({ onSubmit, onBack, error, busy }: {
  onSubmit: (code: string) => void;
  onBack: () => void;
  error: string | null;
  busy: boolean;
}) {
  const t = useT();
  const [code, setCode] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (isPlausibleCode(code)) onSubmit(cleanCode(code));
  };
  return (
    <form className="login-card" onSubmit={submit} aria-label={t("components.loginCode.aria")}>
      <div className="brand big"><span className="brand-mark">M</span><div><strong>SMO</strong><span>{t("components.loginCode.product")}</span></div></div>
      <p className="muted">{t("components.loginCode.intro")}</p>
      <label className="field"><span className="field-label">{t("components.loginCode.aria")}</span>
        <input autoFocus autoComplete="one-time-code" inputMode="text" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)}
               placeholder="123456" aria-describedby="code-hint" required />
        <span className="field-hint" id="code-hint">{t("components.loginCode.hint")}</span>
      </label>
      {error && <div className="error-box" role="alert">{error}</div>}
      <button className="btn primary block" disabled={busy || !isPlausibleCode(code)}>{busy ? t("components.checking") : t("components.loginCode.verify")}</button>
      <button type="button" className="btn ghost block" onClick={onBack}>{t("components.loginCode.back")}</button>
    </form>
  );
}
