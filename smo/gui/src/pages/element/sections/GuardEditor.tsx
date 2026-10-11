/** Element detail · guard editor (`element.guard-editor`): set the guard of one cell (`PUT /managed-entities/{me}/cells/{cell}/guards`: class,
 * sector group, incident zone, neighbour cells) or remove it (`DELETE`, asked first). Opens for the cell in `?cell=` ("+" for a new cell id),
 * starting from the guard the element holds (`GET /managed-entities/{me}`). Shown only to a role that may write guards (admin, `Can`). */
import { useEffect, useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { ActionButton, Can, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { Segmented } from "../../../kit/Segmented";
import { guardPath, useEditedCell, useEntity } from "../data/queries";
import { CELL_CLASSES, type CellClass } from "../data/types";

/** The PUT body from the editor's text: blank group and zone are null, neighbours one per line (or comma-separated), duplicates dropped. */
export function guardBody(f: { cellClass: CellClass; sectorGroup: string; incidentZone: string; neighbours: string }) {
  const neighbourRefs = [...new Set(f.neighbours.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
  return { cellClass: f.cellClass, sectorGroup: f.sectorGroup.trim() || null, incidentZone: f.incidentZone.trim() || null, neighbourRefs };
}

/** The editor of element `me`, or nothing when no cell is open. */
export function GuardEditor({ me }: { me: string }) {
  const [cell, close] = useEditedCell();
  const t = useT();
  const entity = useEntity(me);
  const isNew = cell === "+";
  const current = cell && !isNew ? entity.data?.cellGuards[cell] : undefined;
  const [cellId, setCellId] = useState("");
  const [cellClass, setCellClass] = useState<CellClass>("NORMAL");
  const [sectorGroup, setSectorGroup] = useState("");
  const [incidentZone, setIncidentZone] = useState("");
  const [neighbours, setNeighbours] = useState("");
  const [saved, setSaved] = useState(false);
  const save = useSmoAction();
  useEffect(() => {
    setCellId(isNew ? "" : cell ?? "");
    setCellClass(current?.cellClass ?? "NORMAL");
    setSectorGroup(current?.sectorGroup ?? "");
    setIncidentZone(current?.incidentZone ?? "");
    setNeighbours((current?.neighbourRefs ?? []).join("\n"));
    setSaved(false);
  }, [cell, isNew, JSON.stringify(current ?? null)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!cell) return null;
  const target = cellId.trim();
  return (
    <Can method="PUT" path={guardPath(me, target || "cell")}>
      <Card section="element.guard-editor" title={isNew ? t("element.editor.titleNew") : t("element.editor.titleEdit", { cell })} actions={<button type="button" className="btn small ghost" onClick={() => close(null)}>{t("element.editor.close")}</button>}>
        <form className="form" onSubmit={(e) => {
          e.preventDefault();
          if (!target) return;
          save.mutate({ method: "PUT", path: guardPath(me, target), json: guardBody({ cellClass, sectorGroup, incidentZone, neighbours }), success: t("element.editor.saved.toast", { cell: target }) },
            { onSuccess: () => setSaved(true) });
        }}>
          {isNew && <Field label={t("element.editor.cellId")}><input className="mono" value={cellId} onChange={(e) => setCellId(e.target.value)} required /></Field>}
          <Field label={t("element.editor.cellClass")}><Segmented label={t("element.editor.cellClass")} value={cellClass} onChange={setCellClass} options={CELL_CLASSES.map((c) => ({ id: c, label: c }))} /></Field>
          <Field label={t("element.editor.sectorGroup")} hint={t("element.editor.sectorGroup.hint")}><input className="mono" value={sectorGroup} onChange={(e) => setSectorGroup(e.target.value)} /></Field>
          <Field label={t("element.editor.incidentZone")}><input className="mono" value={incidentZone} onChange={(e) => setIncidentZone(e.target.value)} /></Field>
          <Field label={t("element.editor.neighbours")} hint={t("element.editor.neighbours.hint")}>
            <textarea rows={4} className="mono" value={neighbours} onChange={(e) => setNeighbours(e.target.value)} spellCheck={false} />
          </Field>
          <div className="row wrap">
            <button type="submit" className="btn primary" disabled={!target || save.isPending}>{save.isPending ? "…" : t("element.editor.save")}</button>
            {!isNew && current && <ActionButton label={t("element.editor.remove")} tone="danger" confirm={t("element.editor.remove.confirm", { cell })}
              action={{ method: "DELETE", path: guardPath(me, cell), success: t("element.editor.removed", { cell }) }} onDone={() => close(null)} />}
          </div>
          {saved && <Callout tone="volt">{t("element.editor.saved")}</Callout>}
        </form>
      </Card>
    </Can>
  );
}
