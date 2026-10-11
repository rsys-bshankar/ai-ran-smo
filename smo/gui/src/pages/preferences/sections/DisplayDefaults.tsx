/** Preferences › Display defaults (section `preferences.defaults`): start page, rows per page, time zone, clock, reduce motion, alarm sound. */
import { Card, Field } from "../../../components/ui";
import { CHOICES, type Preferences } from "../../../data/preferences";
import { navKey, useT } from "../../../i18n";

/** The defaults form; every change goes to the page's draft (saved by the page's Save button). */
export function DisplayDefaults({ value, onChange }: { value: Preferences; onChange: (p: Partial<Preferences>) => void }) {
  const t = useT();
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <Card title={t("preferences.defaults.title")} sub={t("preferences.defaults.sub")} section="preferences.defaults">
      <div className="grid g2">
        <Field label={t("preferences.defaults.startPage")}>
          <select value={value.startPage} onChange={(e) => onChange({ startPage: e.target.value })}>
            {CHOICES.startPage.map((p) => <option key={p} value={p}>{t(navKey(p))}</option>)}
          </select>
        </Field>
        <Field label={t("preferences.defaults.rows")}>
          <select value={value.rowsPerPage} onChange={(e) => onChange({ rowsPerPage: Number(e.target.value) as Preferences["rowsPerPage"] })}>
            {CHOICES.rowsPerPage.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </Field>
        <Field label={t("preferences.defaults.timeZone")}>
          <select value={value.timeZone} onChange={(e) => onChange({ timeZone: e.target.value as Preferences["timeZone"] })}>
            <option value="local">{t("preferences.defaults.browserZone", { zone })}</option><option value="UTC">UTC</option>
          </select>
        </Field>
        <Field label={t("preferences.defaults.clock")}>
          <select value={value.clock} onChange={(e) => onChange({ clock: e.target.value as Preferences["clock"] })}>
            <option value="24h">{t("preferences.defaults.clock24")}</option><option value="12h">{t("preferences.defaults.clock12")}</option>
          </select>
        </Field>
      </div>
      <label className="check"><input type="checkbox" checked={value.reduceMotion} onChange={(e) => onChange({ reduceMotion: e.target.checked })} /> {t("preferences.defaults.reduceMotion")}</label>
      <label className="check"><input type="checkbox" checked={value.alarmSound} onChange={(e) => onChange({ alarmSound: e.target.checked })} /> {t("preferences.defaults.alarmSound")}</label>
    </Card>
  );
}
