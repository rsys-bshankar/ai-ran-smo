/** The three status tiles of Account security (`security.tiles`, handoff `Security.dc.html`): whether the one-time code is on, how many recovery
 * codes are left, and how this user signs in (local account or the identity provider). Read from `/api/me` (the session) and `/api/me/totp`. */
import { useAuth } from "../../../auth/AuthContext";
import { useT } from "../../../i18n";
import { Kpi } from "../../../kit/Kpi";
import { useTotpStatus } from "../data/queries";

/** The tiles. A value not known yet (or a status that failed to load: the recovery box shows that error with a retry) reads "—". */
export function StatusTiles() {
  const { me } = useAuth();
  const t = useT();
  const status = useTotpStatus();
  const s = status.data;
  const sso = me?.local === false || s?.reason === "identity provider";
  const twoStep = sso ? t("security.tiles.twoStep.provider") : !s ? null : s.enrolled ? t("security.tiles.twoStep.on") : s.pending ? t("security.tiles.twoStep.started") : t("security.tiles.twoStep.off");
  const twoStepFoot = sso ? t("security.tiles.twoStep.providerFoot")
    : !s ? undefined : s.enrolled ? t("security.tiles.twoStep.onFoot") : !s.available ? t("security.tiles.twoStep.unavailable") : t("security.tiles.twoStep.setUp");
  return (
    <div className="grid g3" data-section="security.tiles">
      <Kpi label={t("security.tiles.twoStep")} value={twoStep} foot={twoStepFoot} tone={!sso && s && !s.enrolled ? "warm" : s?.enrolled ? "volt" : undefined} />
      <Kpi label={t("security.tiles.recovery")} value={sso || !s ? null : s.enrolled ? s.recoveryCodesLeft : null}
        foot={sso ? t("security.tiles.recovery.sso") : s?.enrolled ? (s.recoveryCodesLeft <= 2 ? t("security.tiles.recovery.low") : t("security.tiles.recovery.once")) : t("security.tiles.recovery.notYet")}
        tone={s?.enrolled && s.recoveryCodesLeft <= 2 ? "warm" : undefined} />
      <Kpi label={t("security.tiles.method")} value={me ? (sso ? t("security.tiles.method.sso") : t("security.tiles.method.local")) : null}
        foot={me ? <>{t("security.tiles.method.signedInAs")} <strong>{me.username}</strong> · {me.role}</> : undefined} />
    </div>
  );
}
