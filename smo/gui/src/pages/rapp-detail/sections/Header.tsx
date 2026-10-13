/** The rApp detail header (handoff `RappDetail.dc.html`): name and version, owner, instance id, state and autonomy, the autonomy mode as a
 * read-only segmented control (rApp Management has no route that changes the mode of a running instance: it is fixed at CreateInstance), Pin
 * (`/api/me/pins`) and Stop rApp (with a reason, confirm dialog) / Resume — the same safeguard calls the Safeguards page makes
 * (`PUT|DELETE /rapp-mgmt/instances/{id}/kill`). scripts/gui_rapp_pages_e2e.py reads the pin button's "☆ Pin" / "★ Pinned" wording. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { useSmoAction } from "../../../api/hooks";
import { MAX_PINS, usePinToggle, usePins, type RappPage } from "../../../api/rapps";
import { ActionButton, Can, Field, Id, Modal, PageHeader, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Segmented } from "../../../kit/Segmented";
import { instanceBase } from "../../rapps/data/queries";
import { useSafeguards } from "../data/queries";

/** The three autonomy modes, in order of how much the rApp may do alone (each one's name: `rappDetail.header.mode.<mode>`). */
const MODES = ["SHADOW", "ASSIST", "AUTONOMOUS"] as const;

/** The page header of rApp `data`. */
export function Header({ data }: { data: RappPage }) {
  const t = useT();
  const pins = usePins();
  const toggle = usePinToggle();
  const safeguards = useSafeguards(data.instanceId);
  const [stopping, setStopping] = useState(false);
  const pinnedCount = pins.data?.items.length ?? 0;
  const sg = safeguards.data;
  const killPath = `${instanceBase(data.instanceId)}/kill`;
  return (
    <>
      <PageHeader eyebrow={<Link to="/rapps">{t("rappDetail.header.rapps")}</Link>} title={`${data.name ?? t("rappDetail.title")} ${data.version ?? ""}`.trim()}
        subtitle={<>{data.vendor ?? t("rappDetail.header.unknownOwner")} · <Id value={data.instanceId} /> · <StateBadge state={data.state} /> · <StateBadge state={data.autonomyMode} />
          {sg?.killed && <> · <StateBadge state="DISABLED" /> {t("rappDetail.header.writesStopped")}</>}</>}
        actions={<>
          <Segmented label={t("rappDetail.header.mode")} options={MODES.map((m) => ({ id: m, label: t(`rappDetail.header.mode.${m}`), title: t("rappDetail.header.mode.fixed") }))}
            value={data.autonomyMode ?? ""} onChange={() => {}} disabled />
          <button type="button" className="btn" aria-pressed={data.pinned} disabled={toggle.isPending || (!data.pinned && pinnedCount >= MAX_PINS)}
            title={data.pinned ? t("rappDetail.header.unpinTitle") : pinnedCount >= MAX_PINS ? t("rappDetail.header.pinLimit", { max: MAX_PINS }) : t("rappDetail.header.pinTitle")}
            onClick={() => toggle.mutate({ instanceId: data.instanceId, pin: !data.pinned })}>{data.pinned ? t("rappDetail.header.pinned") : t("rappDetail.header.pin")}</button>
          {sg?.invokerId && (sg.killed
            ? <ActionButton label={t("rappDetail.header.resume")} confirm={t("rappDetail.header.resumeConfirm")} action={{ method: "DELETE", path: killPath, success: t("rappDetail.header.resumed") }} />
            : <Can method="PUT" path={killPath}><button type="button" className="btn danger" onClick={() => setStopping(true)}>{t("rappDetail.header.stop")}</button></Can>)}
          {data.operatorApiRegistered && data.state !== "UNDEPLOYED" && (
            <ActionButton label={t("rappDetail.header.renewCredentials")} title={t("rappDetail.header.renewCredentialsTitle")}
              confirm={t("rappDetail.header.renewCredentialsConfirm")}
              action={{ method: "POST", path: `${instanceBase(data.instanceId)}/credentials/renew`, success: t("rappDetail.header.credentialsRenewed") }} />)}
          <Link className="btn" to="/rapps">{t("rappDetail.back")}</Link>
        </>} />
      <p className="gap-note">{t("rappDetail.header.modeNote")}</p>
      {stopping && <StopDialog name={data.name ?? t("rappDetail.stop.thisRapp")} path={killPath} onClose={() => setStopping(false)} />}
    </>
  );
}

/** "Stop <rApp>?" with a reason: its writes are refused until resumed; the instance keeps running. */
function StopDialog({ name, path, onClose }: { name: string; path: string; onClose: () => void }) {
  const t = useT();
  const [reason, setReason] = useState("");
  const action = useSmoAction();
  return (
    <Modal title={t("rappDetail.stop.title", { name })} onClose={onClose}>
      <p className="muted small">{t("rappDetail.stop.body")}</p>
      <Field label={t("rappDetail.stop.reason")} hint={t("rappDetail.stop.reasonHint")}>
        <input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} autoFocus />
      </Field>
      <div className="row gap end">
        <button type="button" className="btn ghost" onClick={onClose}>{t("rappDetail.stop.cancel")}</button>
        <button type="button" className="btn danger" disabled={action.isPending}
          onClick={() => action.mutate({ method: "PUT", path, json: { reason: reason.trim() || null }, success: t("rappDetail.stop.stopped") }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("rappDetail.stop.confirm")}
        </button>
      </div>
    </Modal>
  );
}
