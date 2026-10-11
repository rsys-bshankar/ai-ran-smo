/** The Exports page (route /exports, GUI-9.5b, Account group): the asynchronous CSV exports the user started from the Decisions page and Admin →
 * Audit log (an admin sees every user's), with their state, rows and size, followed every 2 s while one is running, the download of a finished
 * file (kept 24 h) and delete. Data and rules: `data/exports.ts`. Sections and known limits: README.md. Every word comes from the catalog
 * (`i18n/en/exports.ts`, GUI-6.4). */
import { Link } from "react-router-dom";

import { PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { ExportList } from "./sections/ExportList";

/** The page. */
export function Exports() {
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("exports.eyebrow")} title={t("exports.title")}
        subtitle={<>{t("exports.subtitle.before")}<Link to="/decisions">{t("exports.subtitle.decisions")}</Link>{t("exports.subtitle.after")}</>} />
      <SectionBoundary id="exports.list"><ExportList /></SectionBoundary>
    </>
  );
}
