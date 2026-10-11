/** The Users box of Admin (`admin.users`, handoff `Admin.dc.html`): every GUI user with avatar, role (editable), sign-in method (local or the
 * identity provider), status, one-time code state, break-glass flag and the user actions (reset password, revoke sessions, reset the one-time code,
 * (de)activate, delete), and when each was last active (the newest audit row of the user: sign-ins, changes, refusals; plain reads are not
 * audited) and last signed in (`lastActiveAt`, `lastSignInAt` of `GET /api/admin/users`, GUI-9.8), and the regions and tenants each is limited to (GUI-5). */
import { useState } from "react";

import type { GuiUser } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ROLES } from "../../../auth/rbac";
import { Card, DataTable, StateBadge } from "../../../components/ui";
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
  const { me } = useAuth();
  const users = useAdminUsers();
  const m = useBffMutation();
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<string | null>(null);
  const [scoping, setScoping] = useState<GuiUser | null>(null);
  const patch = (username: string, json: Record<string, unknown>, success: string) => m.mutate({ path: `/admin/users/${username}`, opts: { method: "PATCH", json }, success });
  const post = (path: string, ask: string, success: string) => { if (window.confirm(ask)) m.mutate({ path, opts: { method: "POST" }, success }); };
  return (
    <Card section="admin.users" title="Users" sub={users.data ? `${users.data.length} users · SSO users appear on first sign-in` : "GUI users held by the BFF"}
      actions={<button type="button" className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={16} />Add user</button>}>
      <DataTable rows={users.data} loading={users.isLoading} error={users.error} rowKey={(u) => u.username} empty="No users." columns={[
        { header: "User", render: (u) => (
          <div className="row"><span className="av" aria-hidden>{initials(u.username)}</span>
            <div><strong className="small">{u.username}</strong>{u.username === me?.username && <span className="muted small"> (you)</span>}</div></div>
        ) },
        { header: "Role", render: (u) => (
          <select value={u.role} aria-label={`Role for ${u.username}`} onChange={(e) => patch(u.username, { role: e.target.value }, `${u.username} is now ${e.target.value}`)}>
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
        ) },
        { header: "Sign-in", render: (u) => isSsoUser(u) ? <Badge tone="info">SSO</Badge> : <Badge tone="mute">Local</Badge> },
        { header: "Status", render: (u) => <StateBadge state={u.active ? "ACTIVE" : "DISABLED"} /> },
        { header: "Scope", render: (u) => (
          <button type="button" className="btn ghost small" aria-label={`${describeUserScope(u.scope)}: scope of ${u.username}`} title="The regions and tenants this user sees" onClick={() => setScoping(u)}>
            {u.scope === "INVALID" ? <span className="t-bad">{describeUserScope(u.scope)}</span> : describeUserScope(u.scope)}
          </button>
        ) },
        { header: "One-time code", render: (u) => <Badge tone={isSsoUser(u) ? "mute" : u.totpEnrolled ? "ok" : "warn"}>{mfaLabel(u)}</Badge> },
        { header: "Break-glass", render: (u) => (
          <input type="checkbox" checked={Boolean(u.breakGlass)} disabled={isSsoUser(u)} aria-label={`Break-glass for ${u.username}`}
                 onChange={(e) => patch(u.username, { breakGlass: e.target.checked }, `${u.username} ${e.target.checked ? "is now" : "is no longer"} a break-glass account`)} />
        ) },
        { header: <span title="The user's newest audited action (reads are not audited)">Last active</span>,
          render: (u) => u.lastActiveAt ? <span className="mono small">{formatTime(u.lastActiveAt)}</span> : <span className="muted">never</span> },
        { header: "Last sign-in", render: (u) => u.lastSignInAt ? <span className="mono small">{formatTime(u.lastSignInAt)}</span> : <span className="muted">never</span> },
        { header: "Created", render: (u) => <span className="mono small">{formatTime(u.createdAt)}</span> },
        { header: "", className: "actions", render: (u) => (
          <div className="row gap end">
            <button type="button" className="btn small" onClick={() => setResetting(u.username)}>Reset password</button>
            <button type="button" className="btn small" onClick={() => post(`/admin/users/${u.username}/revoke-sessions`, `End every session of ${u.username}?`, `Sessions of ${u.username} ended`)}>Revoke sessions</button>
            {u.totpEnrolled && <button type="button" className="btn small" onClick={() => post(`/admin/users/${u.username}/reset-totp`, `Remove the one-time code and recovery codes of ${u.username}? They sign in with the password alone until they enrol a new one.`, `One-time code of ${u.username} removed`)}>Reset one-time code</button>}
            <button type="button" className="btn small" onClick={() => patch(u.username, { active: !u.active }, `${u.username} ${u.active ? "deactivated" : "activated"}`)}>{u.active ? "Deactivate" : "Activate"}</button>
            {u.username !== me?.username && <button type="button" className="btn small danger" onClick={() => window.confirm(`Delete user ${u.username}?`) && m.mutate({ path: `/admin/users/${u.username}`, opts: { method: "DELETE" }, success: `${u.username} deleted` })}>Delete</button>}
          </div>
        ) },
      ]} />
      <p className="muted small">Role changes apply on the user's next request; password resets, deactivation and &quot;Revoke sessions&quot; end their existing sessions. A break-glass account can sign in with its password and one-time code even when the console accepts only the identity provider; it needs a one-time code to sign in at all.</p>
      {creating && <CreateUser onClose={() => setCreating(false)} />}
      {resetting && <ResetPassword username={resetting} onClose={() => setResetting(null)} />}
      {scoping && <EditScope username={scoping.username} scope={scoping.scope} onClose={() => setScoping(null)} />}
    </Card>
  );
}
