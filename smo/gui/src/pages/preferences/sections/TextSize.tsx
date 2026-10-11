/** Preferences › Text size (section `preferences.size`): the root font size, so every page's text scales together. */
import { Card } from "../../../components/ui";
import { CHOICES, type Preferences } from "../../../data/preferences";
import { useT } from "../../../i18n";
import { Segmented } from "../../../kit/Segmented";

/** The text-size choice, with a sample line to judge it by. */
export function TextSize({ value, onChange }: { value: Preferences["size"]; onChange: (v: Preferences["size"]) => void }) {
  const t = useT();
  return (
    <Card title={t("preferences.size.title")} sub={t("preferences.size.sub")} section="preferences.size">
      <Segmented label={t("preferences.size.title")} value={value} onChange={onChange} options={CHOICES.size.map((s) => ({ id: s, label: t(`preferences.size.${s}`) }))} />
      <div className="inset"><strong>{t("preferences.size.sample")}</strong><div className="small muted">{t("preferences.size.sampleNote")}</div></div>
    </Card>
  );
}
