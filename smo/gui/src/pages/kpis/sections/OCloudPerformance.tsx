/** Section `kpis.ocloud` (O-Cloud tab): FOCOM O-Cloud performance metrics as a server-paged table, filtered by resource on the server
 * (`resource_ref`). */
import { useState } from "react";

import type { OCloudMetric } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { OCLOUD_PERFORMANCE } from "../data/queries";

/** The tab. */
export function OCloudPerformance() {
  const t = useT();
  const [resource, setResource] = useState("");
  return (
    <Card section="kpis.ocloud" title={t("kpis.ocloud.title")} actions={<input value={resource} onChange={(e) => setResource(e.target.value)} placeholder={t("kpis.ocloud.filter")} aria-label={t("kpis.ocloud.filter")} />}>
      <ServerTable<OCloudMetric> path={OCLOUD_PERFORMANCE} query={{ resource_ref: resource.trim() || undefined }} rowKey={(m) => `${m.resourceRef}/${m.metricName}/${m.value}`} empty={t("kpis.ocloud.empty")} columns={[
        { header: t("kpis.ocloud.resource"), render: (m) => <code>{m.resourceRef}</code> }, { header: t("kpis.ocloud.metric"), render: (m) => m.metricName }, { header: t("kpis.ocloud.value"), render: (m) => m.value },
      ]} />
    </Card>
  );
}
