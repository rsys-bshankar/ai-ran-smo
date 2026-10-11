/** The rApp detail "Faults" box: the faults the rApp reported, paged by rApp Management (`/rapp-mgmt/instances/{id}/faults`, SCALE.md P1).
 * Section id `rapp.faults`. */
import type { FaultReport } from "../../../api/types";
import { Card, SeverityChip } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { faultsPath } from "../data/queries";

/** The box of instance `id`. */
export function Faults({ id }: { id: string }) {
  const t = useT();
  return (
    <Card section="rapp.faults" title={t("rappDetail.faults.title")}>
      <ServerTable<FaultReport> path={faultsPath(id)} rowKey={(f) => f.faultId} empty={t("rappDetail.faults.empty")} columns={[
        { header: t("rappDetail.faults.severity"), render: (f) => <SeverityChip severity={f.severity} /> },
        { header: t("rappDetail.faults.description"), render: (f) => f.description ?? "—" },
        { header: t("rappDetail.faults.reported"), render: (f) => formatTime(f.reportedAt) },
      ]} />
    </Card>
  );
}
