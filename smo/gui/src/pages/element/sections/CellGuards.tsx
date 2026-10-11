/** Element detail · cell guards (`element.guards`): the guard of each cell of this element, paged by the server
 * (`GET /cell-guards?managed_element_ref=`): its class (NORMAL, COVERAGE_CRITICAL, EMERGENCY), sector group, incident zone and neighbours.
 * Every rApp reads these before it acts on a cell. "Edit" and "Add a cell guard" open the editor (`?cell=`); they show only to a role that may
 * set a guard (admin in gui-bff/app/rbac.py). */
import { Can, Card, type Column } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { ServerTable } from "../../../kit/ServerTable";
import { GUARDS_PATH, guardPath, useEditedCell } from "../data/queries";
import type { CellGuardRow } from "../data/types";

/** The tone of a cell class. */
export const CLASS_TONE = { NORMAL: "mute", COVERAGE_CRITICAL: "warn", EMERGENCY: "bad" } as const;

/** The guards card of element `me`. */
export function CellGuards({ me }: { me: string }) {
  const [cell, edit] = useEditedCell();
  const t = useT();
  const columns: Column<CellGuardRow>[] = [
    { header: t("element.guards.cell"), render: (g) => <span className="mono small">{g.cellId}</span> },
    { header: t("element.guards.class"), render: (g) => <Badge tone={CLASS_TONE[g.cellClass] ?? "mute"}>{g.cellClass}</Badge> },
    { header: t("element.guards.sectorGroup"), render: (g) => <span className="mono small">{g.sectorGroup ?? "—"}</span> },
    { header: t("element.guards.incidentZone"), render: (g) => <span className="mono small">{g.incidentZone ?? "—"}</span> },
    { header: t("element.guards.neighbours"), render: (g) => <span className="small">{g.neighbourRefs.length}{g.neighbourRefs.length ? ` · ${g.neighbourRefs.slice(0, 2).join(", ")}${g.neighbourRefs.length > 2 ? "…" : ""}` : ""}</span> },
    { header: "", className: "actions", render: (g) => (
      <Can method="PUT" path={guardPath(me, g.cellId)}><button type="button" className="btn small" onClick={() => edit(g.cellId)}>{t("element.guards.edit")}</button></Can>
    ) },
  ];
  return (
    <div className="stack" data-section="element.guards">
      <Callout tone="volt">{t("element.guards.callout")}</Callout>
      <Card title={t("element.guards.title")} sub={t("element.guards.sub", { me, cell: "{cell}" })}
        actions={<Can method="PUT" path={guardPath(me, "new")}><button type="button" className="btn small" onClick={() => edit("+")}>{t("element.guards.add")}</button></Can>}>
        <ServerTable<CellGuardRow> path={GUARDS_PATH} query={{ managed_element_ref: me }} columns={columns} rowKey={(g) => g.cellId} selectedKey={cell}
          empty={t("element.guards.empty")} />
      </Card>
    </div>
  );
}
