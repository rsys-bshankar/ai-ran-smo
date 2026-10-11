/** The Users box of Admin (`admin.users`, handoff `Admin.dc.html`): every GUI user with avatar, role (editable), sign-in method (local or the
 * identity provider), status, one-time code state, break-glass flag and the user actions (reset password, revoke sessions, reset the one-time code,
 * (de)activate, delete), and when each was last active (the newest audit row of the user: sign-ins, changes, refusals; plain reads are not
 * audited) and last signed in (`lastActiveAt`, `lastSignInAt` of `GET /api/admin/users`, GUI-9.8), and the regions and tenants each is limited to (GUI-5). */
import { useState } from "react";

import type { GuiUser } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ROLES } from "../../../auth/rbac";
import { Card, DataTable, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Icon } from "../../../kit/icons";
import { describeUserScope, formatTime } from "../../../lib/domain";
import { mfaLabel } from "../../../lib/mfa";
import { useAdminUsers, useBffMutation } from "../data/queries";
import { CreateUser, EditScope, ResetPassword } from "./UserDialogs";

/** True for a user the identity provider created (`oidc:<subject>`), who signs in by single sign-on. */
export const isSsoUser = (u: Pick<GuiUser, "username">) => u.username.startsWith("oidc:");

/** Two letters for the avatar: the initials of the name after any `oidc:` prefix. */
function initials(username: string): string {
  const name = username.replace(/^oidc:/, "");
  const parts = name.split(/[._\-@\s]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? parts[0]?.[1] ?? "")).toUpperCase();
}

/** The table, the "Add user" button and the two dialogs. */
export function UsersTable() {
  const t = useT();
  const { me } = useAuth();
  const users = useAdminUsers();
  const m = useBffMutation();
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<string | null>(null);
  const [scoping, setScoping] = useState<GuiUser | null>(null);
  const patch = (username: string, json: Record<string, unknown>, success: string) => m.mutate({ path: `/admin/users/${username}`, opts: { method: "PATCH", json }, success });
  const post = (path: string, ask: string, success: string) => { if (window.confirm(ask)) m.mutate({ path, opts: { method: "POST" }, success }); };
  return (
    <Card section="admin.users" title={t("admin.users.title")} sub={users.data ? t("admin.users.sub", { count: users.data.length }) : t("admin.users.sub.loading")}
      actions={<button type="button" className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={16} />{t("admin.users.add")}</button>}>
      <DataTable rows={users.data} loading={users.isLoading} error={users.error} rowKey={(u) => u.username} empty={t("admin.users.empty")} columns={[
        { header: t("admin.users.col.user"), render: (u) => (
          <div className="row"><span className="av" aria-hidden>{initials(u.username)}</span>
            <div><strong className="small">{u.username}</strong>{u.username === me?.username && <span className="muted small"> {t("admin.users.you")}</span>}</div></div>
        ) },
        { header: t("admin.users.col.role"), render: (u) => (
          <select value={u.role} aria-label={t("admin.users.role.aria", { user: u.username })} onChange={(e) => patch(u.username, { role: e.target.value }, t("admin.users.role.changed", { user: u.username, role: e.target.value }))}>
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
        ) },
        { header: t("admin.users.col.signIn"), render: (u) => isSsoUser(u) ? <Badge tone="info">SSO</Badge> : <Badge tone="mute">{t("admin.users.local")}</Badge> },
        { header: t("admin.users.col.status"), render: (u) => <StateBadge state={u.active ? "ACTIVE" : "DISABLED"} /> },
        { header: t("admin.users.col.scope"), render: (u) => (
          <button type="button" className="btn ghost small" aria-label={t("admin.users.scope.aria", { scope: describeUserScope(u.scope, t), user: u.username })} title={t("admin.users.scope.title")} onClick={() => setScoping(u)}>
            {u.scope === "INVALID" ? <span className="t-bad">{describeUserScope(u.scope, t)}</span> : describeUserScope(u.scope, t)}
          </button>
        ) },
        { header: t("admin.users.col.mfa"), render: (u) => <Badge tone={isSsoUser(u) ? "mute" : u.totpEnrolled ? "ok" : "warn"}>{mfaLabel(u, t)}</Badge> },
        { header: t("admin.users.col.breakGlass"), render: (u) => (
          <input type="checkbox" checked={Boolean(u.breakGlass)} disabled={isSsoUser(u)} aria-label={t("admin.users.breakGlass.aria", { user: u.username })}
                 onChange={(e) => patch(u.username, { breakGlass: e.target.checked }, t(e.target.checked ? "admin.users.breakGlass.on" : "admin.users.breakGlass.off", { user: u.username }))} />
        ) },
        { header: <span title={t("admin.users.col.lastActive.title")}>{t("admin.users.col.lastActive")}</span>,
          render: (u) => u.lastActiveAt ? <span className="mono small">{formatTime(u.lastActiveAt)}</span> : <span className="muted">{t("admin.users.never")}</span> },
        { header: t("admin.users.col.lastSignIn"), render: (u) => u.lastSignInAt ? <span className="mono small">{formatTime(u.lastSignInAt)}</span> : <span className="muted">{t("admin.users.never")}</span> },
        { header: t("admin.users.col.created"), render: (u) => <span className="mono small">{formatTime(u.createdAt)}</span> },
        { header: "", className: "actions", render: (u) => (
          <div className="row gap end">
            <button type="button" className="btn small" onClick={() => setResetting(u.username)}>{t("admin.users.resetPassword")}</button>
            <button type="button" className="btn small" onClick={() => post(`/admin/users/${u.username}/revoke-sessions`, t("admin.users.revoke.confirm", { user: u.username }), t("admin.users.revoke.done", { user: u.username }))}>{t("admin.users.revoke")}</button>
            {u.totpEnrolled && <button type="button" className="btn small" onClick={() => post(`/admin/users/${u.username}/reset-totp`, t("admin.users.resetTotp.confirm", { user: u.username }), t("admin.users.resetTotp.done", { user: u.username }))}>{t("admin.users.resetTotp")}</button>}
            <button type="button" className="btn small" onClick={() => patch(u.username, { active: !u.active }, t(u.active ? "admin.users.deactivated" : "admin.users.activated", { user: u.username }))}>{t(u.active ? "admin.users.deactivate" : "admin.users.activate")}</button>
            {u.username !== me?.username && <button type="button" className="btn small danger" onClick={() => window.confirm(t("admin.users.delete.confirm", { user: u.username })) && m.mutate({ path: `/admin/users/${u.username}`, opts: { method: "DELETE" }, success: t("admin.users.deleted", { user: u.username }) })}>{t("admin.users.delete")}</button>}
          </div>
        ) },
      ]} />
      <p className="muted small">{t("admin.users.note")}</p>
      {creating && <CreateUser onClose={() => setCreating(false)} />}
      {resetting && <ResetPassword username={resetting} onClose={() => setResetting(null)} />}
      {scoping && <EditScope username={scoping.username} scope={scoping.scope} onClose={() => setScoping(null)} />}
    </Card>
  );
}
