/** The owner of a model as the AI/ML page shows it (SEC-15.3c): the owner's invoker ID, or, for a model registered before owners were recorded, a warning badge
 * "No owner (open to any rApp)" so those legacy models can be found in the table and set by an admin (`SetOwner`). */
import { useT } from "../../../i18n";

/** The owner, or the badge for none. */
export function OwnerCell({ owner }: { owner: string | null }) {
  const t = useT();
  return owner ? <>{owner}</> : <span className="badge b-warn tone-warn" title={t("aiml.owner.none.hint")}>{t("aiml.owner.none")}</span>;
}
