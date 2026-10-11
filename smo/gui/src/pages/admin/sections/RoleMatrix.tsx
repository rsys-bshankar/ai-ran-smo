/** The role matrix (`admin.roles`): what each GUI role may do, drawn from the BFF's own permission table (GUI-10.4). Each row names a few calls
 * that stand for it (`ROLE_ROWS`); a role gets a tick when the live table (`GET /api/permissions`, gui-bff/app/rbac.py `RULES`) allows it every
 * one of them, so the card cannot drift from the rules. The BFF's own routes (users, audit log, export jobs) are not in that table; their rows
 * carry the role the BFF checks in code (`bff`). The BFF re-checks every call, so this card explains and never grants. */
import { useAuth } from "../../../auth/AuthContext";
import { can, ROLES, type PermissionRule, type Role } from "../../../auth/rbac";
import { Card } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";

/** One row: the catalog key of what it means, and either the calls that stand for it (checked against the table) or the minimum role of a
 * BFF-own route. */
export interface RoleRow { what: TKey; probes?: [string, string][]; bff?: Role }

/** The rows, in order of the role they need. */
export const ROLE_ROWS: RoleRow[] = [
  { what: "admin.roles.row.read", probes: [["GET", "/rapp-mgmt/instances"], ["GET", "/ran-nf-oam/alarms"], ["GET", "/mlmr/models"]] },
  { what: "admin.roles.row.lifecycle",
    probes: [["POST", "/onboarding/packages/x/prime"], ["POST", "/rapp-mgmt/instances"], ["POST", "/aimgf/training-jobs"], ["PATCH", "/ran-nf-oam/alarms/x/ack"],
      ["POST", "/ran-nf-oam/config-jobs"], ["POST", "/ran-nf-oam/pm-subscriptions"], ["POST", "/ran-nf-oam/fm-subscriptions"], ["DELETE", "/ran-nf-oam/fm-subscriptions/x"],
      ["POST", "/intent-service/intents"], ["POST", "/so-smos/orders"], ["POST", "/ran-nf-oam/rapp-approvals/x/approve"], ["PUT", "/rapp-mgmt/kill-all"]] },
  { what: "admin.roles.row.featureGroups", probes: [["GET", "/aimgf/feature-groups"]] },
  { what: "admin.roles.row.exportDecisions", bff: "operator" },
  { what: "admin.roles.row.teardown",
    probes: [["DELETE", "/onboarding/packages/x"], ["POST", "/rapp-mgmt/instances/x/terminate"], ["DELETE", "/rapp-mgmt/instances/x"], ["DELETE", "/mlmr/models/x"],
      ["DELETE", "/intent-service/intents/x"], ["DELETE", "/nfo/deployments/x"], ["POST", "/focom/resources/provision"], ["DELETE", "/focom/resources/x"]] },
  { what: "admin.roles.row.registry",
    probes: [["POST", "/sme/provider-registrations"], ["POST", "/sme/invoker-registrations"], ["POST", "/ran-nf-oam/msac/roles"], ["PUT", "/ran-nf-oam/rapp-limits/x"], ["DELETE", "/rapp-mgmt/kill-all"]] },
  { what: "admin.roles.row.testData", probes: [["POST", "/ran-nf-oam/alarms/ingest"], ["POST", "/focom/alarms/ingest"], ["POST", "/rapp-mgmt/instances/x/performance"]] },
  { what: "admin.roles.row.users", bff: "admin" },
];

/** Whether `role` may do what `row` stands for under `rules`: every probe allowed (a probe no rule matches is refused), or the BFF-own role. */
export function rowAllows(rules: PermissionRule[], row: RoleRow, role: Role): boolean {
  if (row.bff) return ROLES.indexOf(role) >= ROLES.indexOf(row.bff);
  return (row.probes ?? []).every(([method, path]) => can(rules, role, method, path));
}

/** One cell: a tick with the word for screen readers, or a dash. */
function Mark({ yes }: { yes: boolean }) {
  const t = useT();
  return yes ? <span className="t-ok" aria-label={t("admin.roles.yes")}>✓</span> : <span className="faint" aria-label={t("admin.roles.no")}>—</span>;
}

/** The card; while the table is not read yet it says so instead of showing an all-dash matrix. */
export function RoleMatrix() {
  const t = useT();
  const { rules } = useAuth();
  return (
    <Card section="admin.roles" title={t("admin.roles.title")} sub={t("admin.roles.sub")}>
      {rules.length === 0 ? <p className="small muted">{t("admin.roles.loading")}</p> : (
        <div className="table-wrap" tabIndex={0}>
          <table className="table matrix">
            <thead><tr><th>{t("admin.roles.col.can")}</th><th>{t("admin.roles.col.viewer")}</th><th>{t("admin.roles.col.operator")}</th><th>{t("admin.roles.col.admin")}</th></tr></thead>
            <tbody>
              {ROLE_ROWS.map((row) => (
                <tr key={row.what}><td>{t(row.what)}{row.bff && <span className="xs muted"> {t("admin.roles.bffRoute")}</span>}</td>
                  {ROLES.map((r) => <td key={r}><Mark yes={rowAllows(rules, row, r)} /></td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
