/**
 * The one-time code (TOTP) set-up of the signed-in local account, shown on the Account security page (`pages/security/sections/Authenticator.tsx`): begin (the BFF returns the secret and the otpauth link, there is
 * no QR code), confirm with a code from the app, then the recovery codes (`RecoveryCodes`), which are shown only once and held in this component's state until the user confirms they saved them.
 * Also shows how many recovery codes are left and generates new ones (which needs a current code). For an identity-provider user there is nothing to set up and it says so.
 * It talks to /api/me/totp/*; the wording of the errors is the backend's `detail`/`title`.
 */

import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api/client";
import type { TotpBegin, TotpConfirmed, TotpStatus } from "../api/types";
import { useT } from "../i18n";
import { cleanCode, groupSecret, isTotpCode, recoveryCodesText, recoveryLeftText } from "../lib/mfa";
import { Card } from "./ui";

/** Shown once, after the confirmation: the recovery codes cannot be read again, so leaving takes an explicit "I have saved them". */
export function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(recoveryCodesText(codes)); setCopied(true); } catch { setCopied(false); }
  };
  return (
    <div className="form" role="region" aria-label={t("components.totp.recovery.title")}>
      <div className="error-box" role="alert">{t("components.totp.recovery.save")}</div>
      <ul className="recovery-codes">{codes.map((c) => <li key={c}><code>{c}</code></li>)}</ul>
      <div className="row gap">
        <button type="button" className="btn" onClick={copy}>{copied ? t("components.totp.recovery.copied") : t("components.totp.recovery.copyAll")}</button>
        <button type="button" className="btn primary" onClick={onDone}>{t("components.totp.recovery.saved")}</button>
      </div>
    </div>
  );
}

/**
 * A one-field form for a six-digit code with its own submit button and error line; it calls `onSubmit` only with a code that has six digits (spaces and dashes removed).
 */
function CodeForm({ label, button, busy, error, onSubmit }: { label: string; button: string; busy: boolean; error: string | null; onSubmit: (code: string) => void }) {
  const t = useT();
  const [code, setCode] = useState("");
  const submit = (e: FormEvent) => { e.preventDefault(); if (isTotpCode(code)) onSubmit(cleanCode(code)); };
  return (
    <form className="form inline" onSubmit={submit}>
      <label className="field"><span className="field-label">{label}</span>
        <input autoComplete="one-time-code" inputMode="numeric" maxLength={7} placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} required />
      </label>
      <button className="btn primary" disabled={busy || !isTotpCode(code)}>{busy ? t("components.checking") : button}</button>
      {error && <div className="error-box" role="alert" style={{ flexBasis: "100%" }}>{error}</div>}
    </form>
  );
}

const reason = (err: unknown): string => (err instanceof ApiError ? (err.detail ?? err.title) : (err as Error).message);

/** Account security (PR-SEC-7.1, 7.3): set up the one-time code of the signed-in local account, see the recovery codes left, make new ones. */
export function TotpEnrolment({ required = false }: { required?: boolean }) {
  const t = useT();
  const qc = useQueryClient();
  const status = useQuery<TotpStatus, ApiError>({ queryKey: ["bff", "totp"], queryFn: () => api<TotpStatus>("/me/totp"), retry: false });
  const [begun, setBegun] = useState<TotpBegin | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["bff", "totp"] }); qc.invalidateQueries({ queryKey: ["bff", "me"] }); };

  const begin = useMutation<TotpBegin, ApiError>({
    mutationFn: () => api<TotpBegin>("/me/totp/begin", { method: "POST" }),
    onSuccess: (b) => { setBegun(b); setError(null); refresh(); },
    onError: (e) => setError(reason(e)),
  });
  const confirm = useMutation<TotpConfirmed, ApiError, string>({
    mutationFn: (code) => api<TotpConfirmed>("/me/totp/confirm", { method: "POST", json: { code } }),
    // the secret is not needed any more, and the recovery codes are held in this component only until the user says they are saved
    onSuccess: (r) => { setBegun(null); setCodes(r.recoveryCodes); setError(null); },
    onError: (e) => setError(reason(e)),
  });
  const regenerate = useMutation<TotpConfirmed, ApiError, string>({
    mutationFn: (code) => api<TotpConfirmed>("/me/totp/recovery-codes", { method: "POST", json: { code } }),
    onSuccess: (r) => { setCodes(r.recoveryCodes); setError(null); },
    onError: (e) => setError(reason(e)),
  });

  if (status.isLoading) return <Card title={t("components.totp.title")}><p className="muted">{t("components.loading")}</p></Card>;
  if (status.error || !status.data) return <Card title={t("components.totp.title")}><div className="error-box" role="alert">{status.error ? reason(status.error) : t("components.totp.couldNotLoad")}</div></Card>;
  const s = status.data;

  if (codes) {
    return <Card title={t("components.totp.recovery.title")}><RecoveryCodes codes={codes} onDone={() => { setCodes(null); refresh(); }} /></Card>;
  }
  if (s.reason === "identity provider") {
    return <Card title={t("components.totp.title")}><p>{t("components.totp.idp")}</p></Card>;
  }
  return (
    <Card title={t("components.totp.titleApp")}>
      {required && <div className="error-box" role="alert">{t("components.totp.required")}</div>}
      {s.enrolled ? (
        <>
          <p>{t("components.totp.enrolled")} {recoveryLeftText(s.recoveryCodesLeft, t)}</p>
          <h3>{t("components.totp.newCodes")}</h3>
          <p className="muted small">{t("components.totp.newCodes.note")}</p>
          <CodeForm label={t("components.totp.currentCode")} button={t("components.totp.generate")} busy={regenerate.isPending} error={error} onSubmit={(c) => regenerate.mutate(c)} />
          <p className="muted small">{t("components.totp.newDevice")}</p>
        </>
      ) : !s.available ? (
        <div className="error-box" role="alert">{t("components.totp.unavailable")}</div>
      ) : !begun ? (
        <>
          <p>{t("components.totp.protect")}</p>
          {error && <div className="error-box" role="alert">{error}</div>}
          <button className="btn primary" onClick={() => begin.mutate()} disabled={begin.isPending}>{s.pending ? t("components.totp.startAgain") : t("components.totp.setUp")}</button>
        </>
      ) : (
        <div className="form">
          <p>{t("components.totp.setupHelp")}</p>
          <label className="field"><span className="field-label">{t("components.totp.setupKey")}</span>
            <input readOnly value={groupSecret(begun.secret)} aria-label={t("components.totp.setupKey")} onFocus={(e) => e.currentTarget.select()} style={{ fontFamily: "monospace" }} />
          </label>
          <details>
            <summary className="muted small">{t("components.totp.otpauth")}</summary>
            <input readOnly value={begun.otpauthUri} aria-label={t("components.totp.otpauth.aria")} onFocus={(e) => e.currentTarget.select()} style={{ width: "100%", fontFamily: "monospace" }} />
          </details>
          <p className="muted small">{t("components.totp.notActive")}</p>
          <CodeForm label={t("components.totp.codeFromApp")} button={t("components.totp.confirm")} busy={confirm.isPending} error={error} onSubmit={(c) => confirm.mutate(c)} />
        </div>
      )}
    </Card>
  );
}
