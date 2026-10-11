/** RAN topology page (route /topology, BRIEF §4e feature 1, handoff `Topology.dc.html`): relation counts, a focus-one-element neighbour graph,
 * a containment relation check, the focused element's summary, the containment tree with its alarm overlay (GUI-3) and the table of relations
 * that need attention. Layout only: each box is a
 * section under `sections/`, its data in `data/queries.ts`; the focused element and the problem filter live in the URL (`?me=`, `?problem=`).
 * The Dashboard's "Open topology" link lands here. Sections and limits: README.md. */
import { Link } from "react-router-dom";

import { PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { ContainmentGraph } from "./sections/ContainmentGraph";
import { ElementSummary } from "./sections/ElementSummary";
import { ExportTeiv } from "./sections/ExportTeiv";
import { NeighbourGraph } from "./sections/NeighbourGraph";
import { ProblemRelations } from "./sections/ProblemRelations";
import { RelationCheck } from "./sections/RelationCheck";
import { RelationTiles } from "./sections/RelationTiles";

/** The RAN topology page. */
export function Topology() {
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("topology.eyebrow")} title={t("topology.title")}
        subtitle={t("topology.subtitle")}
        actions={<><SectionBoundary id="topology.export"><ExportTeiv /></SectionBoundary><Link className="btn" to="/">{t("topology.back")}</Link></>} />
      <SectionBoundary id="topology.tiles"><RelationTiles /></SectionBoundary>
      <div className="grid g-main-side">
        <SectionBoundary id="topology.graph"><NeighbourGraph /></SectionBoundary>
        <div className="stack">
          <SectionBoundary id="topology.check"><RelationCheck /></SectionBoundary>
          <SectionBoundary id="topology.element"><ElementSummary /></SectionBoundary>
        </div>
      </div>
      <SectionBoundary id="topology.containment"><ContainmentGraph /></SectionBoundary>
      <SectionBoundary id="topology.problems"><ProblemRelations /></SectionBoundary>
    </>
  );
}
