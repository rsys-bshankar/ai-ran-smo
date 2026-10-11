/** "Export…" (GUI-9.5b): the button that turns a page's current filters into an asynchronous export job (`data/exports.ts`). It opens a dialog
 * that lists what the file will hold (and which of the page's filters it cannot take), starts the job, and then points to the Exports page,
 * where the file is downloaded when it is written. Used by the Decisions page, the alarm table (GUI-2.5) and Admin → Audit log. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { exportErrorText, useCreateExport, type ExportJob, type ExportRequest } from "../data/exports";
import { navKey, useT, type TKey } from "../i18n";
import { KeyValue, Modal } from "./ui";

/** The request fields the dialog lists, in order; each one's label is `components.export.field.<field>`. */
const FIELDS: (keyof ExportRequest)[] = [
  "since", "until", "invokerId", "disposition", "region", "siteCluster", "username", "action", "severity", "ackState", "openOnly", "probableCause",
  "managedElementRef", "managedFunctionRef",
];
/** The catalog key of a request field's label. */
const fieldKey = (k: keyof ExportRequest) => `components.export.field.${k}` as TKey;

/** Props: the request the page's filters make, the button's title, and a note on the filters the export does not take (null: none). */
export interface ExportJobButtonProps { request: ExportRequest; what: string; note?: ReactNode }

/** The button and its dialog. */
export function ExportJobButton({ request, what, note }: ExportJobButtonProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const create = useCreateExport();
  const [job, setJob] = useState<ExportJob | null>(null);
  const close = () => { setOpen(false); setJob(null); create.reset(); };
  const start = () => create.mutate(request, { onSuccess: (j) => setJob(j) });
  const rows = FIELDS.filter((k) => request[k]).map((k) => [t(fieldKey(k)), k === "since" && request.since.startsWith("1970-") ? t("components.export.firstRecord") : request[k] === true ? t("components.export.yes") : String(request[k])] as [string, string]);
  if (!request.until) rows.splice(1, 0, [t("components.export.field.until"), t("components.export.untilNow")]);
  return (
    <>
      <button type="button" className="btn small" onClick={() => setOpen(true)} title={t("components.export.button.title")}>{t("components.export.button")}</button>
      {open && (
        <Modal title={t("components.export.title", { what })} onClose={close}>
          {job ? (
            <div className="stack">
              <p>{t("components.export.started")} <strong>{job.state.toLowerCase()}</strong>{t("components.export.started.where")}{" "}
                <Link to="/exports" onClick={close}>{t(navKey("/exports"))}</Link> {t("components.export.started.when")}</p>
              <div className="row end"><button type="button" className="btn" onClick={close}>{t("components.close")}</button><Link className="btn primary" to="/exports" onClick={close}>{t("components.export.open")}</Link></div>
            </div>
          ) : (
            <div className="stack">
              <p className="small muted">{t("components.export.what")}</p>
              <KeyValue items={rows} />
              {note && <p className="small muted" role="note">{note}</p>}
              {create.error && <div className="error-box" role="alert">{exportErrorText(create.error, t)}</div>}
              <div className="row gap end">
                <button type="button" className="btn" onClick={close}>{t("components.cancel")}</button>
                <button type="button" className="btn primary" disabled={create.isPending} onClick={start}>{t("components.export.start")}</button>
              </div>
            </div>
          )}
        </Modal>
      )}
    </>
  );
}
