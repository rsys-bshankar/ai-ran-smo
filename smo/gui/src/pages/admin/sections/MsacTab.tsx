/** The "RAN access control (MSAC)" tab of Admin (`admin.msac`, BRIEF §4e feature 5): RAN NF OAM's roles, identities (USERNAME, EMAIL_ADDRESS,
 * PHONE_NUMBER, IP_ADDRESS, MACHINEUSER) and access rules (ALLOW or DENY of operations on a data-node selector), each a server-paged table. MSAC
 * decides what a config write may touch; the GUI roles decide what a person may click. An admin creates roles, identities and rules (MsacForms.tsx)
 * and deletes them; both are role-gated (`Can`, `ActionButton`) on the BFF's admin-only MSAC write rules (GUI-9.7). */
import { ActionButton, Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { ServerTable } from "../../../kit/ServerTable";
import { MSAC } from "../data/queries";
import type { MsacAccessRule, MsacIdentity, MsacRole } from "../data/types";
import { NewIdentity, NewRole, NewRule } from "./MsacForms";

/** A delete button for one MSAC object (shown only when the BFF allows the call); `name` is already in the reader's language. */
function Delete({ path, id, name }: { path: string; id: string; name: string }) {
  const t = useT();
  return <ActionButton tone="danger" label={t("admin.msac.delete")} confirm={t("admin.msac.delete.confirm", { name })} action={{ method: "DELETE", path: `${path}/${id}`, success: t("admin.msac.deleted", { name }) }} />;
}

/** The tab: an explanation and the three tables. */
export function MsacTab() {
  const t = useT();
  return (
    <div className="stack" data-section="admin.msac">
      <Callout tone="volt" title={t("admin.msac.title")}>
        {t("admin.msac.intro")}
      </Callout>
      <div className="grid g2">
        <Card section="admin.msac.roles" title={t("admin.msac.roles.title")} sub={t("admin.msac.roles.sub")}>
          <NewRole />
          <ServerTable<MsacRole> path={MSAC.roles} rowKey={(r) => r.id} empty={t("admin.msac.roles.empty")} columns={[
            { header: t("admin.msac.col.role"), render: (r) => <strong>{r.attributes.roleName}</strong> },
            { header: t("admin.msac.col.accessRules"), render: (r) => <span className="num">{r.attributes.accessRulesList.length}</span> },
            { header: t("admin.msac.col.id"), render: (r) => <Id value={r.id} /> },
            { header: "", className: "actions", render: (r) => <Delete path={MSAC.roles} id={r.id} name={t("admin.msac.delete.role", { name: r.attributes.roleName })} /> },
          ]} />
        </Card>
        <Card section="admin.msac.identities" title={t("admin.msac.identities.title")} sub={t("admin.msac.identities.sub")}>
          <NewIdentity />
          <ServerTable<MsacIdentity> path={MSAC.identities} rowKey={(i) => i.id} empty={t("admin.msac.identities.empty")} columns={[
            { header: t("admin.msac.col.identity"), render: (i) => <strong className="mono small">{i.attributes.identityName}</strong> },
            { header: t("admin.msac.col.type"), render: (i) => <Badge tone="info" plain>{i.attributes.identityType}</Badge> },
            { header: t("admin.msac.col.roles"), render: (i) => <span className="num">{i.attributes.roleList.length}</span> },
            { header: "", className: "actions", render: (i) => <Delete path={MSAC.identities} id={i.id} name={t("admin.msac.delete.identity", { name: i.attributes.identityName })} /> },
          ]} />
        </Card>
      </div>
      <Card section="admin.msac.rules" title={t("admin.msac.rules.title")} sub={t("admin.msac.rules.sub")}>
        <NewRule />
        <ServerTable<MsacAccessRule> path={MSAC.rules} rowKey={(r) => r.id} empty={t("admin.msac.rules.empty")} columns={[
          { header: t("admin.msac.col.rule"), render: (r) => <strong>{r.attributes.ruleName}</strong> },
          { header: t("admin.msac.col.action"), render: (r) => <Badge tone={r.attributes.actions === "ALLOW" ? "ok" : "bad"}>{r.attributes.actions}</Badge> },
          { header: t("admin.msac.col.operations"), render: (r) => <span className="row wrap">{r.attributes.operations.map((o) => <span key={o} className="chip">{o}</span>)}</span> },
          { header: t("admin.msac.col.dataNodes"), render: (r) => <code className="small">{r.attributes.dataNodeSelector}</code> },
          { header: "", className: "actions", render: (r) => <Delete path={MSAC.rules} id={r.id} name={t("admin.msac.delete.rule", { name: r.attributes.ruleName })} /> },
        ]} />
      </Card>
    </div>
  );
}
