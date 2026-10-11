/** Preferences › Preview (section `preferences.preview`): a few of the console's primitives drawn with the previewed preferences (they are
 * already on `<html>`), so the user sees tiles, chips, badges and buttons before saving. Sample content only, labelled as such. The badges show
 * state names as the modules send them (RUNNING, PENDING, AUTONOMOUS), which stay untranslated everywhere. */
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Kpi } from "../../../kit/Kpi";
import { Meter } from "../../../kit/Meter";

/** The preview card. */
export function LivePreview() {
  const t = useT();
  return (
    <Card title={t("preferences.preview.title")} sub={t("preferences.preview.sub")} section="preferences.preview" className="preview-sticky">
      <div className="preview-box">
        <Kpi label={t("preferences.preview.tile")} value="98.0" unit="%" foot={t("preferences.preview.tileFoot")}>
          <Meter parts={[{ key: "ok", value: 92, tone: "ok" }, { key: "warn", value: 6, tone: "warn" }, { key: "bad", value: 2, tone: "bad" }]} />
        </Kpi>
        <div className="row wrap">
          <span className="sev sev-cr">{t("preferences.preview.critical")}</span><span className="sev sev-mj">{t("preferences.preview.major")}</span>
          <Badge tone="ok">{"RUNNING"}</Badge><Badge tone="warn">{"PENDING"}</Badge><Badge tone="volt">{"AUTONOMOUS"}</Badge>
        </div>
        <div className="row wrap">
          <button type="button" className="btn primary">{t("preferences.preview.approve")}</button>
          <button type="button" className="btn">{t("preferences.preview.open")}</button>
          <button type="button" className="btn danger">{t("preferences.preview.stop")}</button>
        </div>
        <a href="#preview">{t("preferences.preview.link")}</a>
      </div>
      <p className="xs muted">{t("preferences.preview.stored")}</p>
    </Card>
  );
}
