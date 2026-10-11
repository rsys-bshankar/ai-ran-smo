/** Section `intents.handlers` (Handlers tab): the registered intent handlers (RMIH) as a server-paged table with their supported object types,
 * target names, scope and callback, a role-gated Deregister, and the admin-only "register a handler on the framework's behalf" tool. Handlers are
 * framework-internal (SO SMOS / SA SMOS, D-SEC-POLICY-1): Intent Service refuses an rApp id (a UUID). */
import { useState } from "react";

import type { Rmih } from "../../../api/types";
import { ActionButton, Can, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { splitList } from "../../../lib/domain";
import { ServerTable } from "../../../kit/ServerTable";
import { HANDLERS } from "../data/queries";

/** The tab. */
export function Handlers() {
  const t = useT();
  return (
    <Card section="intents.handlers" title={t("intents.handlers.title")} actions={<span className="muted small">{t("intents.handlers.internal")}</span>}>
      <ServerTable<Rmih> path={HANDLERS} rowKey={(h) => h.rmihId} empty={t("intents.handlers.empty")} columns={[
        { header: "RMIH", render: (h) => <strong>{h.rmihId}</strong> },
        { header: t("intents.handlers.col.objectTypes"), render: (h) => (h.attributes?.intentHandlingCapabilityList ?? []).map((c) => c.supportedExpectationObjectType).join(", ") },
        { header: t("intents.handlers.col.targets"), render: (h) => [...new Set((h.attributes?.intentHandlingCapabilityList ?? []).flatMap((c) => c.supportedExpectationTargetInfoList.map((x) => x.supportedTargetName)))].join(", ") || "—" },
        { header: t("intents.col.scope"), render: (h) => h.intentHandlingScope?.join(", ") ?? t("intents.any") },
        { header: t("intents.col.callback"), render: (h) => <code className="small">{h.notificationDestination}</code> },
        { header: "", className: "actions", render: (h) => <ActionButton label={t("intents.handlers.deregister")} tone="danger" confirm={t("intents.handlers.deregister.confirm", { id: h.rmihId })}
          action={{ method: "DELETE", path: `${HANDLERS}/${h.rmihId}`, success: t("intents.handlers.deregistered") }} /> },
      ]} />
      <Can method="POST" path={HANDLERS}><RegisterHandler /></Can>
    </Card>
  );
}

/** The SMO modules that may hold an rmihId (named in the register tool). */
const FRAMEWORK_HANDLERS = ["so-smos", "sa-smos"] as const;

/** The admin tool: one capability per object type, each supporting the listed target names. */
function RegisterHandler() {
  const [f, setF] = useState({ rmihId: "so-smos", smeServiceId: "so-smos-intent-handler", callback: "http://so-smos:8000/intents", types: "RAN_SUBNETWORK", targetNames: "RANEnergyConsumption", scope: "RAN" });
  const t = useT();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const uuidLike = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(f.rmihId);
  return (
    <details className="admin-tools">
      <summary>{t("intents.handlers.register.summary")}</summary>
      <p className="muted small">{t("intents.handlers.register.intro")} <code>{FRAMEWORK_HANDLERS[0]}</code> / <code>{FRAMEWORK_HANDLERS[1]}</code> {t("intents.handlers.register.intro.end")}</p>
      <div className="form grid cols-3 tight">
        <Field label="RMIH ID" hint={uuidLike ? <span className="text-bad">{t("intents.handlers.register.uuid")}</span> : undefined}><input value={f.rmihId} onChange={set("rmihId")} /></Field>
        <Field label={t("intents.handlers.register.sme")}><input value={f.smeServiceId} onChange={set("smeServiceId")} /></Field>
        <Field label={t("intents.handlers.register.callback")}><input value={f.callback} onChange={set("callback")} /></Field>
        <Field label={t("intents.handlers.register.types")} hint={t("intents.handlers.register.types.hint")}><input value={f.types} onChange={set("types")} /></Field>
        <Field label={t("intents.handlers.register.targets")} hint={t("intents.handlers.register.targets.hint")}><input value={f.targetNames} onChange={set("targetNames")} /></Field>
        <Field label={t("intents.handlingScope")}><select value={f.scope} onChange={set("scope")}><option value="">{t("intents.any")}</option><option>RAN</option><option>CN</option></select></Field>
      </div>
      <ActionButton label={t("intents.handlers.register.submit")} disabled={!f.rmihId || !f.types} action={{
        method: "POST", path: HANDLERS, success: t("intents.handlers.registered"),
        json: { rmihId: f.rmihId, smeServiceId: f.smeServiceId, notificationDestination: f.callback,
          intentHandlingCapabilityList: splitList(f.types).map((type) => ({
            intentHandlingCapabilityId: `cap-${type}`, supportedExpectationObjectType: type,
            supportedExpectationTargetInfoList: splitList(f.targetNames).map((n) => ({ supportedTargetName: n })),
          })),
          intentHandlingScope: f.scope ? [f.scope] : null },
      }} />
    </details>
  );
}
