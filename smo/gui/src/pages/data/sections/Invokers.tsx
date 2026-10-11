/** Data & Exposure → SME, the API invokers box (`data.invokers`): onboarded invokers (server-paged), whether each is trusted, "Trust…" (the
 * trusted-invoker security-context form), remove trust, offboard, and the role-gated onboarding form whose one-time secret is shown once. The
 * "with a security context" count is the trusted-invoker list's own total. Moved from the pre-redesign page with its actions unchanged. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { SmeInvoker } from "../../../api/types";
import { ActionButton, Can, Card, Field, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { PATHS, useTrustedCount } from "../data/queries";

/** The names of the onboarding answer's fields, shown as the API names them (not words to translate). */
const FIELD = { apiInvokerId: "apiInvokerId", onboardingSecret: "onboardingSecret" };

/** Example values shown as placeholders (key markers, not words: the same in every language). */
const EXAMPLE = { publicKey: "-----BEGIN PUBLIC KEY-----…" };

/** The box. */
export function Invokers() {
  const t = useT();
  const trusted = useTrustedCount();
  const onboard = useSmoAction();
  const [publicKey, setPublicKey] = useState("");
  const [secret, setSecret] = useState<{ apiInvokerId: string; onboardingSecret: string } | null>(null);
  const [trustFor, setTrustFor] = useState<string | null>(null);
  return (
    <Card section="data.invokers" title={t("data.invokers.title")}>
      <ServerTable<SmeInvoker> path={PATHS.invokers} rowKey={(i) => i.apiInvokerId} empty={t("data.invokers.empty")} columns={[
        { header: t("data.invokers.col.invoker"), render: (i) => <code className="small">{i.apiInvokerId}</code> },
        { header: t("data.invokers.col.auth"), render: (i) => i.keyAuthentication ? t("data.invokers.auth.assertion") : t("data.invokers.auth.secret") },
        { header: t("data.invokers.col.trusted"), render: (i) => i.trusted ? <StateBadge state="ENABLED" /> : <span className="muted">{t("data.invokers.no")}</span> },
        { header: "", className: "actions", render: (i) => <div className="row gap end">
          {i.trusted
            ? <ActionButton label={t("data.invokers.removeTrust")} tone="danger" confirm={t("data.invokers.removeTrust.confirm")} action={{ method: "DELETE", path: `${PATHS.trustedInvokers}/${i.apiInvokerId}`, success: t("data.invokers.trustRemoved") }} />
            : <Can method="PUT" path={`${PATHS.trustedInvokers}/${i.apiInvokerId}`}><button type="button" className="btn small" onClick={() => setTrustFor(i.apiInvokerId)}>{t("data.invokers.trust")}</button></Can>}
          <ActionButton label={t("data.invokers.offboard")} tone="danger" confirm={t("data.invokers.offboard.confirm")} action={{ method: "DELETE", path: `${PATHS.invokers}/${i.apiInvokerId}`, success: t("data.invokers.offboarded") }} />
        </div> },
      ]} />
      <p className="muted small">{t("data.invokers.note", { n: trusted.data?.total ?? "—" })}</p>
      <Can method="POST" path={PATHS.invokers}>
        <div className="form inline">
          <Field label={t("data.invokers.publicKey")}><input value={publicKey} onChange={(e) => setPublicKey(e.target.value)} placeholder={EXAMPLE.publicKey} /></Field>
          <button type="button" className="btn" disabled={!publicKey || onboard.isPending} onClick={() => onboard.mutate(
            { method: "POST", path: PATHS.invokers, json: { apiInvokerPublicKey: publicKey }, success: t("data.invokers.onboarded") },
            { onSuccess: (d) => { setSecret(d as { apiInvokerId: string; onboardingSecret: string }); setPublicKey(""); } })}>{t("data.invokers.onboard")}</button>
        </div>
      </Can>
      {secret && (
        <Modal title={t("data.invokers.secret.title")} onClose={() => setSecret(null)}>
          <p>{t("data.invokers.secret.body")}</p>
          <dl className="kv"><div><dt>{FIELD.apiInvokerId}</dt><dd><code>{secret.apiInvokerId}</code></dd></div><div><dt>{FIELD.onboardingSecret}</dt><dd><code>{secret.onboardingSecret}</code></dd></div></dl>
        </Modal>
      )}
      {trustFor && <TrustInvoker invokerId={trustFor} onClose={() => setTrustFor(null)} />}
    </Card>
  );
}

/** The trusted-invoker security-context form. */
function TrustInvoker({ invokerId, onClose }: { invokerId: string; onClose: () => void }) {
  const t = useT();
  const [dest, setDest] = useState("http://invoker:8000/security-notify");
  const [aefId, setAefId] = useState("");
  const [apiId, setApiId] = useState("");
  const [method, setMethod] = useState("OAUTH");
  const action = useSmoAction();
  return (
    <Modal title={t("data.trust.title")} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        action.mutate({ method: "PUT", path: `${PATHS.trustedInvokers}/${invokerId}`, success: t("data.trust.registered"),
          json: { notificationDestination: dest, securityInfo: [{ aefId: aefId || null, apiId: apiId || null, prefSecurityMethods: [method] }] } }, { onSuccess: onClose });
      }}>
        <p className="muted small">{t("data.invokers.col.invoker")} <code>{invokerId}</code></p>
        <Field label={t("data.notificationDestination")}><input value={dest} onChange={(e) => setDest(e.target.value)} required /></Field>
        <div className="grid cols-3 tight">
          <Field label={t("data.trust.aefId")}><input value={aefId} onChange={(e) => setAefId(e.target.value)} /></Field>
          <Field label={t("data.trust.apiId")}><input value={apiId} onChange={(e) => setApiId(e.target.value)} /></Field>
          <Field label={t("data.trust.method")}><select value={method} onChange={(e) => setMethod(e.target.value)}><option>OAUTH</option><option>PSK</option><option>PKI</option></select></Field>
        </div>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={action.isPending}>{t("data.trust.register")}</button></div>
      </form>
    </Modal>
  );
}
