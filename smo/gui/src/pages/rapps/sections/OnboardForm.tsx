/** "Onboard a package" (call flow 01, first step): posts a CSAR location to Onboarding, which validates it asynchronously; the result shows up
 * as the package state in the packages table. Role-gated by the Packages tab. Section id `rapps.onboard`. */
import { useState, type FormEvent } from "react";

import { useSmoAction } from "../../../api/hooks";
import { Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { PACKAGES_PATH } from "../data/queries";

/** The onboarding form. */
export function OnboardForm() {
  const t = useT();
  const [location, setLocation] = useState("");
  const [applicationType, setApplicationType] = useState("rApp");
  const action = useSmoAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    action.mutate({ method: "POST", path: PACKAGES_PATH, json: { location, applicationType }, success: t("rapps.onboard.accepted") },
      { onSuccess: () => setLocation("") });
  };
  return (
    <Card section="rapps.onboard" title={t("rapps.onboard.title")}>
      <form className="form inline" onSubmit={submit}>
        <Field label={t("rapps.onboard.location")} hint={t("rapps.onboard.location.hint")}>
          <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder={t("rapps.onboard.location.placeholder")} required pattern="https?://.+" />
        </Field>
        <Field label={t("rapps.onboard.type")}><select value={applicationType} onChange={(e) => setApplicationType(e.target.value)}><option value="rApp">{t("rapps.onboard.type.rapp")}</option></select></Field>
        <button type="submit" className="btn primary" disabled={action.isPending}>{t("rapps.onboard.submit")}</button>
      </form>
    </Card>
  );
}
