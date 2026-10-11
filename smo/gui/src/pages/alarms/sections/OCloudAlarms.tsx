/** Section `alarms.ocloud` (tab "O-Cloud"): O-Cloud infrastructure alarms from FOCOM (O2ims), read-only, in the shared server table (SCALE.md P1).
 * Filters are the route's own parameters: `severity` and `resource_ref`. */
import { useState } from "react";

import { POLL } from "../../../api/hooks";
import type { OCloudAlarm } from "../../../api/types";
import { Card, Id, SeverityChip } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { SEVERITIES } from "../../../lib/domain";
import { OCLOUD_ALARMS } from "../data/queries";

/** The O-Cloud alarm table. */
export function OCloudAlarms() {
  const t = useT();
  const [severity, setSeverity] = useState("");
  const [resource, setResource] = useState("");
  return (
    <Card section="alarms.ocloud" title={t("alarms.ocloud.title")} sub={t("alarms.ocloud.sub")} actions={<>
      <input value={resource} onChange={(e) => setResource(e.target.value)} placeholder={t("alarms.ocloud.resource")} aria-label={t("alarms.ocloud.resource")} />
      <select value={severity} onChange={(e) => setSeverity(e.target.value)} aria-label={t("alarms.ocloud.severity.aria")}>
        <option value="">{t("alarms.ocloud.allSeverities")}</option>{[...SEVERITIES, "cleared"].map((s) => <option key={s}>{s}</option>)}
      </select>
    </>}>
      <ServerTable<OCloudAlarm> path={OCLOUD_ALARMS} query={{ severity: severity || undefined, resource_ref: resource.trim() || undefined }}
        refetchInterval={POLL.alarms} rowKey={(a) => a.alarmId} empty={t("alarms.ocloud.empty")} columns={[
          { header: t("alarms.ocloud.severity"), render: (a) => <SeverityChip severity={a.severity} /> },
          { header: t("alarms.ocloud.resource"), render: (a) => <code>{a.resourceRef}</code> },
          { header: t("alarms.ocloud.alarm"), render: (a) => <Id value={a.alarmId} /> },
        ]} />
    </Card>
  );
}
