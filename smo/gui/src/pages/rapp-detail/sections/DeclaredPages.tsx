/** The rApp's own operator page, as its package declares it (PR-GUI-8, ADR 0004), drawn by `components/OperatorUi` DeclaredPage unchanged,
 * below the platform overview. A rApp with no declaration, or one this console cannot read, says so. Section id `rapp.declared`. */
import type { RappPage } from "../../../api/rapps";
import { DeclaredPage } from "../../../components/OperatorUi";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";

/** The declared page of `data`. */
export function DeclaredPages({ data }: { data: RappPage }) {
  const t = useT();
  return (
    <div className="stack" data-section="rapp.declared">
      <h2 className="section-title">{t("rappDetail.declared.title")}</h2>
      {data.declarationState === "declared" && data.declaration && (
        <DeclaredPage instanceId={data.instanceId} declaration={data.declaration} canChange={data.canChange} operatorApiRegistered={data.operatorApiRegistered} />
      )}
      {data.declarationState === "unreadable" && (
        <Card><p className="muted">{t("rappDetail.declared.unreadable")}</p></Card>
      )}
      {data.declarationState === "none" && (
        <Card><p className="muted">{t("rappDetail.declared.none")}</p></Card>
      )}
    </div>
  );
}
