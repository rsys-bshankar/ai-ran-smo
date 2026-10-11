/** Lifecycle failure notices (`software.watchers` on the Software page, `configuration.watchers` on Configuration → Element onboarding;
 * PR-MGT-14.7, MGT-15.6): who is told, by a webhook through RAN NF OAM's delivery queue, when an element's onboarding fails, a software
 * campaign halts or a campaign's rollback fails. Lists `GET /ran-nf-oam/lifecycle-subscriptions`; an admin adds (`POST`) and removes
 * (`DELETE /{id}`) a watcher, role-gated with `Can` / `ActionButton` against the BFF's permission table. The event names in
 * `LIFECYCLE_EVENTS` are the ones RAN NF OAM accepts (ran-nf-oam/app/lifecycle.py); a new event there must be added here. The URL is only
 * trimmed here: RAN NF OAM refuses an unusable or internal address and the refusal is shown as a toast. */
import { useState } from "react";

import { useSmo, useSmoAction } from "../../../api/hooks";
import { ActionButton, Can, Card, DataTable, Field, Modal } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { formatTime } from "../../../lib/domain";

/** The subscription route. */
export const LIFECYCLE_SUBSCRIPTIONS_PATH = "/ran-nf-oam/lifecycle-subscriptions";

/** The events a watcher can ask for, with the catalog key of what each means. */
export const LIFECYCLE_EVENTS: readonly (readonly [string, TKey])[] = [
  ["ONBOARDING_FAILED", "software.watchers.event.ONBOARDING_FAILED"],
  ["CAMPAIGN_HALTED", "software.watchers.event.CAMPAIGN_HALTED"],
  ["CAMPAIGN_ROLLBACK_FAILED", "software.watchers.event.CAMPAIGN_ROLLBACK_FAILED"],
];

/** One watcher as RAN NF OAM lists it; `events` empty means every event. */
interface LifecycleSubscription { subscriptionId: string; callbackUri: string; events: string[]; createdAt: string | null }

/** The watcher card; `section` is the box's id on the page that shows it. */
export function LifecycleWatchers({ section }: { section: string }) {
  const t = useT();
  const subs = useSmo<LifecycleSubscription[]>(LIFECYCLE_SUBSCRIPTIONS_PATH, { limit: 200 });
  const [adding, setAdding] = useState(false);
  return (
    <Card section={section} title={t("software.watchers.title")} sub={t("software.watchers.sub")}
      actions={<Can method="POST" path={LIFECYCLE_SUBSCRIPTIONS_PATH}><button type="button" className="btn primary" onClick={() => setAdding(true)}>{t("software.watchers.add")}</button></Can>}>
      <p className="muted small">
        {t("software.watchers.intro")}
      </p>
      <DataTable rows={subs.data} rowKey={(s) => s.subscriptionId} error={subs.error} loading={subs.isLoading} empty={t("software.watchers.empty")} columns={[
        { header: t("software.watchers.col.callback"), render: (s) => <code className="small">{s.callbackUri}</code> },
        { header: t("software.watchers.col.events"), render: (s) => (s.events.length ? s.events.join(", ") : t("software.watchers.events.all")) },
        { header: t("software.watchers.col.since"), render: (s) => formatTime(s.createdAt) },
        { header: "", className: "actions", render: (s) => <ActionButton action={{ method: "DELETE", path: `${LIFECYCLE_SUBSCRIPTIONS_PATH}/${s.subscriptionId}`, success: t("software.watchers.removed") }} label={t("software.watchers.remove")} tone="danger" confirm={t("software.watchers.remove.confirm")} /> },
      ]} />
      {adding && <AddWatcher onClose={() => setAdding(false)} />}
    </Card>
  );
}

/**
 * The modal that adds a watcher: a callback URL and the events to be told about (none ticked means all). Posts to the subscriptions route and, on success, closes through `onClose`;
 * the button stays disabled while the URL is blank or a call is pending.
 */
function AddWatcher({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [uri, setUri] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const action = useSmoAction();
  const toggle = (e: string) => setEvents((cur) => (cur.includes(e) ? cur.filter((x) => x !== e) : [...cur, e]));
  return (
    <Modal title={t("software.watchers.dialog.title")} onClose={onClose}>
      <Field label={t("software.watchers.dialog.url")} hint={t("software.watchers.dialog.url.hint")}><input value={uri} onChange={(e) => setUri(e.target.value)} placeholder={t("software.watchers.dialog.url.placeholder")} autoFocus /></Field>
      <fieldset className="field">
        <legend className="field-label">{t("software.watchers.dialog.events")}</legend>
        {LIFECYCLE_EVENTS.map(([event, meaning]) => (
          <label key={event} className="row gap small"><input type="checkbox" checked={events.includes(event)} onChange={() => toggle(event)} /> {event} <span className="muted">{t(meaning)}</span></label>
        ))}
      </fieldset>
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending || !uri.trim()} onClick={() => action.mutate(
          { method: "POST", path: LIFECYCLE_SUBSCRIPTIONS_PATH, json: { callbackUri: uri.trim(), events }, success: t("software.watchers.dialog.added") }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("software.watchers.dialog.add")}
        </button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("software.watchers.dialog.cancel")}</button>
      </div>
    </Modal>
  );
}
