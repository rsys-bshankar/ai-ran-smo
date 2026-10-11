/** Preferences › Theme (section `preferences.theme`): dark, light or match the operating system. */
import { Card } from "../../../components/ui";
import { CHOICES, type Preferences } from "../../../data/preferences";
import { useT } from "../../../i18n";

/** The theme choice. */
export function ThemePicker({ value, onChange }: { value: Preferences["theme"]; onChange: (v: Preferences["theme"]) => void }) {
  const t = useT();
  return (
    <Card title={t("preferences.theme.title")} sub={t("preferences.theme.sub")} section="preferences.theme">
      <div className="swatches" role="radiogroup" aria-label={t("preferences.theme.title")}>
        {CHOICES.theme.map((th) => (
          <button key={th} type="button" role="radio" aria-checked={value === th} className={`swatch${value === th ? " on" : ""}`} onClick={() => onChange(th)} style={{ alignItems: "flex-start", minWidth: 160 }}>
            <strong>{t(`preferences.theme.${th}`)}{value === th ? " ✓" : ""}</strong>
            <span className="xs muted">{t(`preferences.theme.${th}.note`)}</span>
          </button>
        ))}
      </div>
    </Card>
  );
}
