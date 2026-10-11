/** Preferences › Accent colour (section `preferences.accent`). The swatch colours are the accents' own fills (styles.css `[data-accent]`); status
 * colours never follow the accent, which the note under the swatches says. */
import { Card } from "../../../components/ui";
import { CHOICES, type Preferences } from "../../../data/preferences";
import { useT } from "../../../i18n";

// The fill of each accent, the same values styles.css defines as --acc (a swatch shows the colour, so it cannot be the current token).
const FILL: Record<Preferences["accent"], string> = { volt: "#c8f051", blue: "#4c9bf5", teal: "#2dd4bf", amber: "#fbbf24", radisys: "#df1f4e" };

/** The accent choice. */
export function AccentPicker({ value, onChange }: { value: Preferences["accent"]; onChange: (v: Preferences["accent"]) => void }) {
  const t = useT();
  return (
    <Card title={t("preferences.accent.title")} sub={t("preferences.accent.sub")} section="preferences.accent">
      <div className="swatches" role="radiogroup" aria-label={t("preferences.accent.title")}>
        {CHOICES.accent.map((a) => (
          <button key={a} type="button" role="radio" aria-checked={value === a} className={`swatch${value === a ? " on" : ""}`} onClick={() => onChange(a)}>
            <i style={{ background: FILL[a] }} />{t(`preferences.accent.${a}`)}
          </button>
        ))}
      </div>
      <p className="small muted">{t("preferences.accent.note")}</p>
    </Card>
  );
}
