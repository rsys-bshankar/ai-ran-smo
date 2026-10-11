/** The Account security page (route /security, PR-SEC-7, handoff `Security.dc.html`): the one-time code of the signed-in local account and the
 * recovery codes behind it. Also where an admin is sent when GUI_ADMIN_MFA_REQUIRED is on. Sections and known limits: README.md. Every word comes
 * from the catalog (`i18n/en/security.ts`, GUI-6.4). */
import { PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { Authenticator } from "./sections/Authenticator";
import { RecoveryCodes } from "./sections/RecoveryCodes";
import { SignIns } from "./sections/SignIns";
import { StatusTiles } from "./sections/StatusTiles";

/** The page. */
export function Security() {
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("security.eyebrow")} title={t("security.title")} subtitle={t("security.subtitle")} />
      <div className="stack">
        <SectionBoundary id="security.tiles"><StatusTiles /></SectionBoundary>
        <SectionBoundary id="security.authenticator"><Authenticator /></SectionBoundary>
        <div className="grid g2">
          <SectionBoundary id="security.recovery"><RecoveryCodes /></SectionBoundary>
          <SectionBoundary id="security.signins"><SignIns /></SectionBoundary>
        </div>
      </div>
    </>
  );
}
