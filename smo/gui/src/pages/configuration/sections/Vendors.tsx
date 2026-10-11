/** Configuration · vendor capabilities (`configuration.vendors`): what each RAN vendor (or digital twin) supports, paged by the server
 * (`GET /vendor-capabilities`): its MnS services, data-model conformance (SPEC, OWN or COMBINED), O1 modes and the schema descriptors its CM
 * writes are checked against. Declaring or removing a vendor is an admin's call; this box only reads (vendor onboarding stays on its API). */
import { Card, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { VENDORS_PATH } from "../data/queries";
import type { VendorCapability } from "../data/types";

/** The tone of a conformance mode. */
const CONFORMANCE_TONE = { SPEC: "ok", COMBINED: "info", OWN: "warn" } as const;

/** "name@revision", or "—". */
const ref = (r: { schemaName: string; revision: string } | null) => (r ? `${r.schemaName}${r.revision ? `@${r.revision}` : ""}` : null);

/** The table's columns, in the language of `t`. */
function columnsOf(t: Translator): Column<VendorCapability>[] {
  return [
    { header: t("configuration.vendors.col.vendor"), render: (v) => <strong>{v.vendorName}</strong> },
    { header: t("configuration.vendors.col.services"), render: (v) => v.supportedServices.join(", ") },
    { header: t("configuration.vendors.col.conformance"), render: (v) => <Badge tone={CONFORMANCE_TONE[v.conformanceMode] ?? "mute"}>{v.conformanceMode}</Badge> },
    { header: t("configuration.vendors.col.modes"), render: (v) => v.supportedVendorModes.join(", ") },
    { header: t("configuration.vendors.col.schema"), render: (v) => <span className="mono small">{[ref(v.schemaRef), ref(v.specSchemaRef)].filter(Boolean).join(" + ") || "—"}</span> },
    { header: t("configuration.vendors.col.updated"), render: (v) => <span className="small muted">{formatTime(v.updatedAt)}</span> },
  ];
}

/** The card. */
export function Vendors() {
  const t = useT();
  return (
    <Card section="configuration.vendors" title={t("configuration.vendors.title")} sub={t("configuration.vendors.sub")}>
      <ServerTable<VendorCapability> path={VENDORS_PATH} columns={columnsOf(t)} rowKey={(v) => v.vendorName} empty={t("configuration.vendors.empty")} />
    </Card>
  );
}
