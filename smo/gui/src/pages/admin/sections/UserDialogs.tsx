/** The Users tab's dialogs (BRIEF §4b "Admin: the Add user dialog"): add a local user, reset a user's password, and (GUI-5) set the regions and tenants a
 * user is limited to. All post to the BFF (`POST /api/admin/users`, `PATCH /api/admin/users/{username}`); a reset ends the user's existing sessions, a scope
 * applies on the user's next request. */
import { useState, type FormEvent } from "react";

import type { AuthzScope, UserScope } from "../../../api/types";
import { ROLES, type Role } from "../../../auth/rbac";
import { Field, Modal } from "../../../components/ui";
import { userScopeFromFields } from "../../../lib/domain";
import { useBffMutation } from "../data/queries";

/** The two scope fields, shared by the Add user and Scope dialogs. */
function ScopeFields({ regions, tenants, onRegions, onTenants }: { regions: string; tenants: string; onRegions: (v: string) => void; onTenants: (v: string) => void }) {
  return (
    <>
      <Field label="Regions" hint="Comma-separated; blank: every region"><input value={regions} onChange={(e) => onRegions(e.target.value)} /></Field>
      <Field label="Tenants" hint="Comma-separated; blank: every tenant"><input value={tenants} onChange={(e) => onTenants(e.target.value)} /></Field>
    </>
  );
}

/** Add a local user with an initial password and a role. Closes on success. */
export function CreateUser({ onClose }: { onClose: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [regions, setRegions] = useState("");
  const [tenants, setTenants] = useState("");
  const m = useBffMutation();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate({ path: "/admin/users", opts: { method: "POST", json: { username, password, role, scope: userScopeFromFields(regions, tenants) } }, success: `User ${username} created` }, { onSuccess: onClose });
  };
  return (
    <Modal title="Add user" onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <Field label="Username" hint="lowercase letters, digits, _ . -"><input value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} required pattern="[a-z][a-z0-9_.\-]{1,31}" /></Field>
        <Field label="Initial password" hint="At least 8 characters"><input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required /></Field>
        <Field label="Role"><select value={role} onChange={(e) => setRole(e.target.value as Role)}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></Field>
        <ScopeFields regions={regions} tenants={tenants} onRegions={setRegions} onTenants={setTenants} />
        <p className="muted small">Users of the identity provider are not added here: they get a row on their first sign-in.</p>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>Cancel</button><button type="submit" className="btn primary" disabled={m.isPending}>Create</button></div>
      </form>
    </Modal>
  );
}

/** Set a new password for `username`; ends their existing sessions. Closes on success. */
export function ResetPassword({ username, onClose }: { username: string; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const m = useBffMutation();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate({ path: `/admin/users/${username}`, opts: { method: "PATCH", json: { password } }, success: `Password reset for ${username}` }, { onSuccess: onClose });
  };
  return (
    <Modal title={`Reset password — ${username}`} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <Field label="New password" hint="Ends the user's existing sessions"><input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required /></Field>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>Cancel</button><button type="submit" className="btn primary" disabled={m.isPending}>Reset</button></div>
      </form>
    </Modal>
  );
}

/** GUI-5: set or clear the regions and tenants `username` is limited to. What the user sees in RAN NF OAM, DME and SME is narrowed to managed elements of those; the
 * other modules have nothing to match and show what they show to everyone. Blank fields remove the limit. Closes on success. */
export function EditScope({ username, scope, onClose }: { username: string; scope: UserScope | undefined; onClose: () => void }) {
  const current: AuthzScope = scope && scope !== "INVALID" ? scope : {};
  const [regions, setRegions] = useState((current.regions ?? []).join(", "));
  const [tenants, setTenants] = useState((current.tenants ?? []).join(", "));
  const m = useBffMutation();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate({ path: `/admin/users/${username}`, opts: { method: "PATCH", json: { scope: userScopeFromFields(regions, tenants) } }, success: `Scope of ${username} saved` }, { onSuccess: onClose });
  };
  return (
    <Modal title={`Scope — ${username}`} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <ScopeFields regions={regions} tenants={tenants} onRegions={setRegions} onTenants={setTenants} />
        <p className="muted small">Applies on the user's next request. An element without a region (or tenant) is not shown to a user limited on that axis. A user of the identity
          provider gets the scope from the token instead when the console is set up to read one (<code>GUI_OIDC_SCOPE_CLAIM</code>).</p>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>Cancel</button><button type="submit" className="btn primary" disabled={m.isPending}>Save</button></div>
      </form>
    </Modal>
  );
}
