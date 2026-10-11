/** RAN topology · relation check (`topology.check`): how managed object A stands to B in the containment tree (`GET /topology/relation`):
 * SAME, ANCESTOR, DESCENDANT, SIBLING, SAME_ELEMENT or DIFFERENT_ELEMENT; 404 when either DN is not in the tree. The inputs start from the
 * focused element's root DN. */
import { useState } from "react";

import { Card, Field } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { rootDn } from "../../element/data/types";
import { useRelation, useTopologyParams } from "../data/queries";

/** What each relation means, in words (its catalog key). */
const MEANING: Record<string, TKey> = {
  SAME: "topology.check.SAME",
  ANCESTOR: "topology.check.ANCESTOR",
  DESCENDANT: "topology.check.DESCENDANT",
  SIBLING: "topology.check.SIBLING",
  SAME_ELEMENT: "topology.check.SAME_ELEMENT",
  DIFFERENT_ELEMENT: "topology.check.DIFFERENT_ELEMENT",
};

/** The check card. */
export function RelationCheck() {
  const t = useT();
  const { me } = useTopologyParams();
  const seed = me ? `${rootDn(me)},` : "ManagedElement=";
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [asked, setAsked] = useState<{ a: string; b: string }>({ a: "", b: "" });
  const relation = useRelation(asked.a, asked.b);
  return (
    <Card section="topology.check" title={t("topology.check.title")} sub={t("topology.check.sub")}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); setAsked({ a: a.trim(), b: b.trim() }); }}>
        <Field label="A (DN)"><input className="mono" value={a} placeholder={seed} onChange={(e) => setA(e.target.value)} /></Field>
        <Field label="B (DN)"><input className="mono" value={b} placeholder={seed} onChange={(e) => setB(e.target.value)} /></Field>
        <div className="row"><button type="submit" className="btn small" disabled={!a.trim() || !b.trim()}>{t("topology.check.button")}</button></div>
      </form>
      {relation.isFetching && !relation.data && <p className="muted small">{t("topology.check.checking")}</p>}
      {relation.error && <Callout tone="warn" title={relation.error.status === 404 ? t("topology.check.notInTree") : t("topology.check.failed")}>{relation.error.message}</Callout>}
      {relation.data && (
        <Callout tone="volt" title={<span className="row wrap"><Badge tone="volt">{relation.data.relation}</Badge>{relation.data.relation in MEANING ? t(MEANING[relation.data.relation]) : ""}</span>} />
      )}
    </Card>
  );
}
