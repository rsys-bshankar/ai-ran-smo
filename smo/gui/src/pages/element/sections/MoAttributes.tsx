/** Element detail · managed-object attributes (`element.attributes`): the object selected in the tree (`?mo=`): its DN, class, id, parent and
 * where the tree learned it (registry or a walk), from `GET /managed-objects/{dn}`. The tree holds no attribute values, so "Read live values"
 * reads them from the element itself (`GET /managed-entities/{me}/config?managed_function_ref=`, a NETCONF get-config or RESTCONF GET) on
 * request. Which attributes are writable is not served by any route (README, Known limits). */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Card, DataTable, KeyValue } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { useLiveConfig, useMo, useSelectedMo } from "../data/queries";
import { functionRefOf } from "../data/types";

/** The attributes card of element `me`. */
export function MoAttributes({ me }: { me: string }) {
  const t = useT();
  const [dn] = useSelectedMo();
  const mo = useMo(dn);
  const [live, setLive] = useState(false);
  useEffect(() => { setLive(false); }, [dn]);
  const fnRef = dn ? functionRefOf(me, dn) : null;
  const config = useLiveConfig(me, fnRef, live && dn !== null);
  if (!dn) return <Card section="element.attributes" title={t("element.attributes.title")}><Empty title={t("element.attributes.pick")} /></Card>;
  const rows = Object.entries(config.data?.attributes ?? {}).map(([k, v]) => ({ k, v }));
  return (
    <Card section="element.attributes" title={<span className="mono">{dn}</span>} sub={mo.data?.class}
      actions={<Link className="btn small" to="/configuration#new">{t("element.attributes.change")}</Link>}>
      {mo.error && <ErrorRetry error={mo.error} onRetry={() => void mo.refetch()} />}
      {!mo.data && !mo.error && <Skeleton lines={3} />}
      {mo.data && <KeyValue items={[[t("element.attributes.class"), mo.data.class], [t("element.attributes.id"), mo.data.id], [t("element.attributes.parent"), mo.data.parentDn && <span className="mono small">{mo.data.parentDn}</span>],
        [t("element.attributes.knownFrom"), mo.data.source === "walk" ? t("element.attributes.knownFrom.walk") : t("element.attributes.knownFrom.registry")], [t("element.attributes.fnRef"), fnRef && <span className="mono small">{fnRef}</span>]]} />}
      <div className="row"><button type="button" className="btn small" onClick={() => (live ? void config.refetch() : setLive(true))} disabled={config.isFetching}>
        {config.isFetching ? t("element.attributes.reading") : live ? t("element.attributes.readAgain") : t("element.attributes.readLive")}</button>
        <span className="small muted">{t("element.attributes.readNote")}</span></div>
      {live && config.error && <ErrorRetry error={config.error} onRetry={() => void config.refetch()} />}
      {config.data && <DataTable rows={rows} rowKey={(r) => r.k} empty={t("element.attributes.empty")} columns={[
        { header: t("element.attributes.attribute"), render: (r) => <span className="mono small">{r.k}</span> },
        { header: t("element.attributes.value"), render: (r) => <span className="mono small">{typeof r.v === "object" ? JSON.stringify(r.v) : String(r.v)}</span> },
      ]} />}
      <p className="gap-note">{t("element.attributes.writableGap")}</p>
    </Card>
  );
}
