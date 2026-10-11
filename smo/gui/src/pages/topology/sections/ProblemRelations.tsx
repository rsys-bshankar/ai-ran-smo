/** RAN topology · relations that need attention (`topology.problems`): the declared neighbour relations that are not reciprocal, point
 * outside what is managed here, or name a cell id several elements claim, everywhere or only around the focused element. Paged and filtered by
 * RAN NF OAM (`/topology/links?reciprocal=false&link_type=…&limit&offset`): a one-way relation is between elements (`INTER_ELEMENT`, the
 * default) or within one (`INTRA_ELEMENT`), picked with "Between". Each row links to the cell guards on the Element page where the fix is made. */
import { useState } from "react";
import { Link } from "react-router-dom";

import type { Query } from "../../../api/client";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Segmented } from "../../../kit/Segmented";
import { ServerTable } from "../../../kit/ServerTable";
import { elementHref, type CellLink } from "../../element/data/types";
import { fixHint } from "../data/graph";
import { LINKS_PATH, useTopologyParams, type ProblemKind } from "../data/queries";

/** The tone of a link type badge. */
const TYPE_TONE = { INTER_ELEMENT: "info", INTRA_ELEMENT: "ok", EXTERNAL: "warn", AMBIGUOUS: "bad" } as const;

/** The route filters of one problem kind (`oneWay` picks which one-way relations: between elements or within one). */
export function problemQuery(kind: ProblemKind, oneWay: "inter" | "intra", me: string | null): Query {
  const where = me ? { managed_element_ref: me } : {};
  if (kind === "external") return { ...where, link_type: "EXTERNAL" };
  if (kind === "ambiguous") return { ...where, link_type: "AMBIGUOUS" };
  return { ...where, reciprocal: false, link_type: oneWay === "inter" ? "INTER_ELEMENT" : "INTRA_ELEMENT" };
}

/** The table card. */
export function ProblemRelations() {
  const t = useT();
  const { me, problem, setProblem } = useTopologyParams();
  const [scope, setScope] = useState<"all" | "focus">("all");
  const [oneWay, setOneWay] = useState<"inter" | "intra">("inter");
  const focused = scope === "focus" && me !== null;
  const yesNo = (v: boolean, l: CellLink) => (l.linkType === "EXTERNAL" || l.linkType === "AMBIGUOUS" ? <span className="muted">—</span> : v ? t("topology.problems.yes") : t("topology.problems.no"));
  return (
    <Card section="topology.problems" title={t("topology.problems.title")} sub={t("topology.problems.sub")}
      actions={<>
        <Segmented<ProblemKind> label={t("topology.problems.problem")} value={problem} onChange={setProblem}
          options={[{ id: "oneway", label: t("topology.problems.oneway") }, { id: "external", label: t("topology.problems.external") }, { id: "ambiguous", label: t("topology.problems.ambiguous") }]} />
        {problem === "oneway" && <Segmented label={t("topology.problems.between")} value={oneWay} onChange={setOneWay}
          options={[{ id: "inter", label: t("topology.problems.elements") }, { id: "intra", label: t("topology.problems.cellsOfOne") }]} />}
        <Segmented label={t("topology.problems.where")} value={scope} onChange={setScope}
          options={[{ id: "all", label: t("topology.problems.everywhere") }, { id: "focus", label: me ? t("topology.problems.around", { me }) : t("topology.problems.aroundFocused"), title: me ? undefined : t("topology.problems.focusFirst") }]} />
      </>}>
      <ServerTable<CellLink> path={LINKS_PATH} query={problemQuery(problem, oneWay, focused ? me : null)} refetchInterval={60_000}
        rowKey={(l) => `${l.aElement}/${l.aCell}>${l.bElement ?? "?"}/${l.bCell}`}
        empty={t(`topology.problems.empty.${problem}`)}
        columns={[
          { header: t("topology.problems.aCell"), render: (l) => <Link to={elementHref(l.aElement, "guards")}>{l.aElement} / {l.aCell}</Link> },
          { header: t("topology.problems.bCell"), render: (l) => <span className="mono small">{l.bElement ? `${l.bElement} / ${l.bCell}` : l.bCell}</span> },
          { header: t("topology.problems.linkType"), render: (l) => <Badge tone={TYPE_TONE[l.linkType]}>{l.linkType}</Badge> },
          { header: t("topology.problems.reciprocal"), render: (l) => (l.linkType === "EXTERNAL" || l.linkType === "AMBIGUOUS" ? <Badge tone="mute">{t("topology.problems.na")}</Badge> : <Badge tone={l.reciprocal ? "ok" : "bad"}>{l.reciprocal ? t("topology.problems.yes") : t("topology.problems.no")}</Badge>) },
          { header: t("topology.problems.sameSectorGroup"), render: (l) => yesNo(l.sameSectorGroup, l) },
          { header: t("topology.problems.sameIncidentZone"), render: (l) => yesNo(l.sameIncidentZone, l) },
          { header: t("topology.problems.fix"), render: (l) => { const f = fixHint(l, t); return f.element ? <Link to={elementHref(f.element, "guards")}>{f.text}</Link> : <span className="muted">{f.text}</span>; } },
        ]} />
    </Card>
  );
}
