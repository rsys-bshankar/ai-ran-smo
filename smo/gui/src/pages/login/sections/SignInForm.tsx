/** The right half of the sign-in page (`login.form`, handoff `Login.dc.html`): the single sign-on button first when the backend offers OIDC, then the
 * local account form, then the note about the one-time code. After a right password for an account with a one-time code the form gives way to
 * `components/LoginCodeStep` (PR-SEC-7.2). With `GUI_LOGIN_MODE=oidc` only the provider's button shows, and a break-glass account reaches the
 * form through a small link (PR-SEC-7.6). The labels "Username", "Password" and the button "Sign in" are what `scripts/gui_e2e.py` looks for.
 * Its words come from the catalog (`i18n/en/login.ts`, GUI-6.4). */
import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/AuthContext";
import { LoginCodeStep } from "../../../components/LoginCodeStep";
import { useToast } from "../../../components/Toast";
import { useT } from "../../../i18n";
import { Icon } from "../../../kit/icons";
import { loginErrorMessage, recoveryLeftText, type LoginChallenge } from "../../../lib/mfa";
import { oidcErrorMessage, oidcLoginHref } from "../../../lib/oidc";
import { useAuthConfig } from "../data/queries";

/**
 * The sign-in form and its code step. The first step posts the username and password through `AuthProvider.login`; an answer with a challenge opens the code step, a session
 * opens the app. A spent challenge (CHALLENGE_INVALID) or a lock-out (429) sends the user back to the password step. With `GUI_LOGIN_MODE=oidc` the password form is hidden
 * behind a small "Break-glass sign-in" link that appears only when the backend says a break-glass account exists. After a recovery code is spent, a toast says how many are left.
 */
export function SignInForm() {
  const { login, loginWithCode } = useAuth();
  const t = useT();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState<LoginChallenge | null>(null);
  const [breakGlassOpen, setBreakGlassOpen] = useState(false);
  const [params] = useSearchParams();
  const authConfig = useAuthConfig();
  const ssoHref = oidcLoginHref(authConfig.data);
  const formIsNormal = authConfig.data?.localLogin !== false;
  // With GUI_LOGIN_MODE=oidc only the provider's button is shown; a break-glass account reaches the form through a small link.
  const breakGlassLink = !formIsNormal && authConfig.data?.breakGlass === true;
  const showPassword = formIsNormal || (breakGlassLink && breakGlassOpen);
  const ssoError = oidcErrorMessage(params.get("oidc_error"), t);

  const fail = (err: unknown) => {
    const e = err as { status?: number; title?: string; message?: string };
    setError(loginErrorMessage(e, t));
    // a challenge that no longer works (expired, spent) or an account that is locked: back to the password
    if (e.title === "CHALLENGE_INVALID" || e.status === 429) setChallenge(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await login(username, password);
      if (next) setChallenge(next);
      setPassword("");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async (code: string) => {
    if (!challenge) return;
    setBusy(true);
    setError(null);
    try {
      const done = await loginWithCode(challenge.challenge, code);
      if (done.recoveryCodesLeft !== undefined) toast.push({ tone: done.recoveryCodesLeft > 2 ? "info" : "error", text: recoveryLeftText(done.recoveryCodesLeft, t) });
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  if (challenge) {
    return (
      <section className="login-form-wrap" data-section="login.form">
        <LoginCodeStep onSubmit={submitCode} onBack={() => { setChallenge(null); setError(null); }} error={error} busy={busy} />
      </section>
    );
  }

  return (
    <section className="login-form-wrap" data-section="login.form">
      <form className="login-card" onSubmit={submit} aria-labelledby="sign-in-h">
        <div className="col" style={{ gap: 6 }}>
          <h2 id="sign-in-h">{t("login.form.title")}</h2>
          <p className="muted">{t("login.form.lead")}</p>
        </div>
        {ssoError && <div className="error-box" role="alert">{ssoError}</div>}
        {ssoHref && (
          // A full navigation, not a fetch: the backend answers with a redirect to the identity provider.
          <a className="btn primary block" href={ssoHref}><Icon name="safeguards" />{t("login.form.sso", { provider: authConfig.data?.oidc.providerName ?? "SSO" })}</a>
        )}
        {ssoHref && formIsNormal && <div className="or-line">{t("login.form.orLocal")}</div>}
        {breakGlassLink && !breakGlassOpen && (
          <button type="button" className="btn ghost small" onClick={() => setBreakGlassOpen(true)}>{t("login.form.breakGlass")}</button>
        )}
        {showPassword && (<>
          {breakGlassLink && <p className="muted small">{t("login.form.breakGlassOnly")}</p>}
          <label className="field"><span className="field-label">{t("login.form.username")}</span>
            <input autoFocus={!ssoHref || breakGlassLink} autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
          </label>
          <label className="field"><span className="field-label">{t("login.form.password")}</span>
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          {error && <div className="error-box" role="alert">{error}</div>}
          <button type="submit" className={`btn block${ssoHref ? "" : " primary"}`} disabled={busy}>{busy ? t("login.form.signingIn") : t("login.form.submit")}</button>
          <div className="callout info">
            <Icon name="security" />
            <span className="small muted">{t("login.form.codeNote")}</span>
          </div>
        </>)}
        {breakGlassLink && <p className="small faint">{t("login.form.breakGlassNote")}</p>}
      </form>
    </section>
  );
}
