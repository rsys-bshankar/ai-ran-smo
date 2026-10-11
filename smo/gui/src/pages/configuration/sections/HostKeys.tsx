/** Configuration · endpoint trust (`configuration.trust`): the O1 adaptor endpoints, paged by the server (`GET /o1-adaptor-endpoints`, filter
 * by health), and for the selected `ssh` endpoint the SSH host keys pinned for it (`GET /o1-adaptor-endpoints/{id}/host-keys`). An ssh endpoint
 * with no key pinned has its connections refused (unless the NETCONF_SSH_KNOWN_HOSTS file lists it), so that is a red alert; an unreachable one
 * says a changed host key is one possible cause. An admin pins (or re-pins) a key (`PUT …/host-keys`, the BFF records the admin as `pinnedBy`) and
 * removes one (`DELETE …/host-keys/{keyType}`): the trust anchor of the SSH connection, so admin only. The backend does not record a key mismatch
 * (README, Known limits). */
import { useState } from "react";
import { Link } from "react-router-dom";

import { ActionButton, Can, Card, DataTable, Field, Id, StateBadge, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { ServerTable } from "../../../kit/ServerTable";
import { Empty, QueryState } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { elementHref } from "../../element/data/types";
import { ENDPOINTS_PATH, hostKeysPath, useHostKeys } from "../data/queries";
import type { Endpoint } from "../data/types";

/** The endpoint table's columns, in the language of `t`. */
function columnsOf(t: Translator): Column<Endpoint>[] {
  return [
    { header: t("configuration.trust.col.endpoint"), render: (e) => <Id value={e.endpointId} /> },
    { header: t("configuration.trust.col.element"), render: (e) => <Link to={elementHref(e.managedElementRef)} onClick={(ev) => ev.stopPropagation()}>{e.managedElementRef}</Link> },
    { header: t("configuration.trust.col.transport"), render: (e) => e.transport },
    { header: t("configuration.trust.col.health"), render: (e) => <StateBadge state={e.healthStatus} /> },
    { header: t("configuration.trust.col.heartbeat"), render: (e) => <span className="small muted">{formatTime(e.lastHeartbeatAt)}</span> },
  ];
}

/** The two cards: endpoints, and the keys of the selected one. */
export function HostKeys() {
  const t = useT();
  const [health, setHealth] = useState("");
  const [selected, setSelected] = useState<Endpoint | null>(null);
  return (
    <div className="grid g-main-side" data-section="configuration.trust">
      <Card title={t("configuration.trust.endpoints")} sub={t("configuration.trust.endpoints.sub")} actions={<label className="row small">{t("configuration.trust.col.health")}
        <select aria-label={t("configuration.trust.col.health")} value={health} onChange={(e) => setHealth(e.target.value)}>
          <option value="">{t("configuration.all")}</option>{["ACTIVE", "DISCOVERED", "DEGRADED", "UNREACHABLE"].map((h) => <option key={h}>{h}</option>)}
        </select></label>}>
        <ServerTable<Endpoint> path={ENDPOINTS_PATH} query={health ? { health_status: health } : undefined} columns={columnsOf(t)} rowKey={(e) => e.endpointId}
          onRowClick={setSelected} selectedKey={selected?.endpointId} empty={t("configuration.trust.endpoints.empty")} />
      </Card>
      {selected ? <Keys endpoint={selected} /> : <Card title={t("configuration.trust.keys")}><Empty title={t("configuration.trust.pick")} /></Card>}
    </div>
  );
}

/** The keys of one endpoint, with the alerts. */
function Keys({ endpoint }: { endpoint: Endpoint }) {
  const t = useT();
  const ssh = endpoint.transport === "ssh";
  const keys = useHostKeys(ssh ? endpoint.endpointId : null);
  return (
    <Card title={t("configuration.trust.keys")} sub={`${endpoint.managedElementRef} · ${endpoint.adaptorUri}`}>
      {!ssh ? <Empty title={t("configuration.trust.notSsh", { transport: endpoint.transport })}>{t("configuration.trust.notSsh.body")}</Empty> : (
        <>
          {endpoint.healthStatus === "UNREACHABLE" && (
            <Callout tone="bad" title={t("configuration.trust.unreachable")}>{t("configuration.trust.unreachable.body")}</Callout>
          )}
          <QueryState q={keys} empty={<Callout tone="bad" title={t("configuration.trust.noKey")}>{t("configuration.trust.noKey.body")}</Callout>}>
            <DataTable rows={keys.data} rowKey={(k) => k.keyType} columns={[
              { header: t("configuration.trust.keyType"), render: (k) => <span className="mono small">{k.keyType}</span> },
              { header: t("configuration.trust.col.fingerprint"), render: (k) => <span className="mono small">{k.fingerprint}</span> },
              { header: t("configuration.trust.col.pinnedBy"), render: (k) => k.pinnedBy },
              { header: t("configuration.trust.col.pinned"), render: (k) => <span className="small muted">{formatTime(k.pinnedAt)}</span> },
              { header: "", className: "actions", render: (k) => <ActionButton label={t("configuration.trust.remove")} tone="danger"
                confirm={t("configuration.trust.remove.confirm", { keyType: k.keyType })}
                action={{ method: "DELETE", path: `${hostKeysPath(endpoint.endpointId)}/${encodeURIComponent(k.keyType)}`, success: t("configuration.trust.removed") }} /> },
            ]} />
          </QueryState>
          <Can method="PUT" path={hostKeysPath(endpoint.endpointId)}><PinKey endpointId={endpoint.endpointId} /></Can>
        </>
      )}
    </Card>
  );
}

/** The SSH key types a NETCONF device presents. */
const KEY_TYPES = ["ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"] as const;

/** The key type and base64 blob of what was pasted: a bare blob keeps `fallbackType`; a `.pub` or known_hosts line gives its own type. */
export function parseKeyInput(text: string, fallbackType: string): { type: string; blob: string } {
  const parts = text.trim().split(/\s+/).filter(Boolean);
  const i = parts.findIndex((t) => /^(ssh-|ecdsa-)/.test(t));
  if (i >= 0 && parts[i + 1]) return { type: parts[i], blob: parts[i + 1] };
  return { type: fallbackType, blob: parts.length === 1 ? parts[0] : "" };
}

/** An admin's form to pin (or replace) the host key of one type; the public key is the base64 blob the device presents. */
function PinKey({ endpointId }: { endpointId: string }) {
  const t = useT();
  const [keyType, setKeyType] = useState<string>(KEY_TYPES[0]);
  const [publicKey, setPublicKey] = useState("");
  // a pasted "ssh-ed25519 AAAA… comment" (.pub) or "host ssh-ed25519 AAAA…" (known_hosts) line is reduced to its key blob and its type
  const { type, blob } = parseKeyInput(publicKey, keyType);
  return (
    <details className="inset" data-part="pin-key">
      <summary className="small">{t("configuration.trust.pin")}</summary>
      <div className="form">
        <Field label={t("configuration.trust.keyType")}><select value={type} onChange={(e) => setKeyType(e.target.value)} aria-label={t("configuration.trust.keyType")}>{KEY_TYPES.map((k) => <option key={k}>{k}</option>)}</select></Field>
        <Field label={t("configuration.trust.publicKey")} hint={t("configuration.trust.publicKey.hint")}><textarea rows={3} className="mono" value={publicKey}
          onChange={(e) => setPublicKey(e.target.value)} aria-label={t("configuration.trust.publicKey")} spellCheck={false} /></Field>
        <div className="row end">
          <ActionButton label={t("configuration.trust.pinKey")} tone="primary" disabled={!blob}
            confirm={t("configuration.trust.pinKey.confirm")}
            action={{ method: "PUT", path: hostKeysPath(endpointId), json: { keyType: type, publicKey: blob, pinnedBy: "smo-gui" }, success: t("configuration.trust.pinned", { keyType: type }) }}
            onDone={() => setPublicKey("")} />
        </div>
      </div>
    </details>
  );
}
