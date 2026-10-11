/** Section `safeguards.watchers` (tab "Watchers"): who is told about refusals — webhooks posted through the delivery queue — with Add (admin) and
 * Remove. The list is a server table. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { RefusalCode, SafeguardSubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime, REFUSAL_CODES, REFUSAL_MEANING } from "../../../lib/domain";
import { WATCHERS } from "../data/queries";

/** The watcher list. */
export function Watchers() {
  const t = useT();
  const [adding, setAdding] = useState(false);
  return (
    <Card section="safeguards.watchers" title={t("safeguards.watchers.title")} actions={
      <Can method="POST" path={WATCHERS}><button type="button" className="btn primary" onClick={() => setAdding(true)}>{t("safeguards.watchers.add")}</button></Can>}>
      <p className="muted small">{t("safeguards.watchers.intro")}</p>
      <ServerTable<SafeguardSubscription> path={WATCHERS} rowKey={(s) => s.subscriptionId} empty={t("safeguards.watchers.empty")} columns={[
        { header: t("safeguards.watchers.callback"), render: (s) => <code className="small">{s.callbackUri}</code> },
        { header: t("safeguards.watchers.refusals"), render: (s) => (s.refusals.length ? s.refusals.join(", ") : t("safeguards.watchers.all")) },
        { header: t("safeguards.watchers.since"), render: (s) => formatTime(s.createdAt) },
        { header: "", render: (s) => <ActionButton action={{ method: "DELETE", path: `${WATCHERS}/${s.subscriptionId}`, success: t("safeguards.watchers.removed") }}
            label={t("safeguards.watchers.remove")} tone="danger" confirm={t("safeguards.watchers.remove.confirm")} /> },
      ]} />
      {adding && <AddWatcher onClose={() => setAdding(false)} />}
    </Card>
  );
}

/** The add-watcher dialog: a callback URL and the refusal codes it wants (none: all). */
function AddWatcher({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [uri, setUri] = useState("");
  const [codes, setCodes] = useState<RefusalCode[]>([]);
  const action = useSmoAction();
  const toggle = (c: RefusalCode) => setCodes((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]));
  return (
    <Modal title={t("safeguards.watchers.dialog.title")} onClose={onClose}>
      <Field label={t("safeguards.watchers.dialog.url")} hint={t("safeguards.watchers.dialog.url.hint")}><input value={uri} onChange={(e) => setUri(e.target.value)} placeholder={t("safeguards.watchers.dialog.url.placeholder")} autoFocus /></Field>
      <fieldset className="field">
        <legend className="field-label">{t("safeguards.watchers.dialog.codes")}</legend>
        {REFUSAL_CODES.map((c) => (
          <label key={c} className="row gap small"><input type="checkbox" checked={codes.includes(c)} onChange={() => toggle(c)} /> {c} <span className="muted">{t(REFUSAL_MEANING[c])}</span></label>
        ))}
      </fieldset>
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending || !uri.trim()} onClick={() => action.mutate(
          { method: "POST", path: WATCHERS, json: { callbackUri: uri.trim(), refusals: codes }, success: t("safeguards.watchers.dialog.success") }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("safeguards.watchers.add")}
        </button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("safeguards.cancel")}</button>
      </div>
    </Modal>
  );
}
