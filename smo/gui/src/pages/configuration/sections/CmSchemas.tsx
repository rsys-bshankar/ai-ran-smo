/** Configuration · CM schemas (`configuration.schemas`): the schema descriptors every config write is checked against (YANG, OPENAPI_NRM or
 * DESCRIPTOR), built-in ones first, paged by the server (`GET /cm-schemas`). Loading one is an admin's call through the API. */
import { Card, type Column } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { ServerTable } from "../../../kit/ServerTable";
import { SCHEMAS_PATH } from "../data/queries";
import type { CmSchema } from "../data/types";

/** The table's columns, in the language of `t`. */
function columnsOf(t: Translator): Column<CmSchema>[] {
  return [
    { header: t("configuration.schemas.col.schema"), render: (s) => <strong className="mono small">{s.schemaName}</strong> },
    { header: t("configuration.schemas.col.type"), render: (s) => <Badge tone="info">{s.type}</Badge> },
    { header: t("configuration.schemas.col.revision"), render: (s) => <span className="mono small">{s.revision || "—"}</span> },
    { header: t("configuration.schemas.col.classes"), render: (s) => s.classCount },
    { header: t("configuration.schemas.col.location"), render: (s) => (s.builtin ? <span className="muted">{t("configuration.schemas.bundled")}</span> : <span className="mono small">{s.location}</span>) },
  ];
}

/** The card. */
export function CmSchemas() {
  const t = useT();
  return (
    <Card section="configuration.schemas" title={t("configuration.schemas.title")} sub={t("configuration.schemas.sub")}>
      <ServerTable<CmSchema> path={SCHEMAS_PATH} columns={columnsOf(t)} rowKey={(s) => `${s.schemaName}@${s.revision}`} empty={t("configuration.schemas.empty")} />
    </Card>
  );
}
