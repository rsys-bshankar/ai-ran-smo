/** "Onboard a package" (call flow 01a, first step): either uploads a .csar file from the browser (`POST /onboarding/packages/upload`, multipart) or posts a CSAR
 * location that Onboarding fetches (`POST /onboarding/packages`). Validation runs inside the request and the verdict is the package state in the packages
 * table. Role-gated by the Packages tab. Section id `rapps.onboard`. */
import { useState, type FormEvent } from "react";

import { useSmoAction } from "../../../api/hooks";
import { Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { PACKAGES_PATH } from "../data/queries";

/** The onboarding form: a file to upload or, when no file is chosen, a URL to fetch from. */
export function OnboardForm() {
  const t = useT();
  const [location, setLocation] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [inputKey, setInputKey] = useState(0);         // a new key empties the file input after a successful upload
  const [applicationType, setApplicationType] = useState("rApp");
  const action = useSmoAction();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (file) {
      const body = new FormData();
      body.append("file", file);
      body.append("applicationType", applicationType);
      action.mutate({ method: "POST", path: `${PACKAGES_PATH}/upload`, body, success: t("rapps.onboard.accepted") },
        { onSuccess: () => { setFile(null); setInputKey((k) => k + 1); } });
      return;
    }
    action.mutate({ method: "POST", path: PACKAGES_PATH, json: { location, applicationType }, success: t("rapps.onboard.accepted") },
      { onSuccess: () => setLocation("") });
  };
  return (
    <Card section="rapps.onboard" title={t("rapps.onboard.title")}>
      <form className="form inline" onSubmit={submit}>
        <Field label={t("rapps.onboard.file")} hint={t("rapps.onboard.file.hint")}>
          <input key={inputKey} type="file" accept=".csar" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </Field>
        <Field label={t("rapps.onboard.location")} hint={t("rapps.onboard.location.hint")}>
          <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder={t("rapps.onboard.location.placeholder")} required={!file}
            disabled={!!file} pattern="https?://.+" />
        </Field>
        <Field label={t("rapps.onboard.type")}><select value={applicationType} onChange={(e) => setApplicationType(e.target.value)}><option value="rApp">{t("rapps.onboard.type.rapp")}</option></select></Field>
        <button type="submit" className="btn primary" disabled={action.isPending}>{file ? t("rapps.onboard.upload") : t("rapps.onboard.submit")}</button>
      </form>
    </Card>
  );
}
