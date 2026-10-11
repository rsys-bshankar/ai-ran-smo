/** The recovery codes box of Account security (`security.recovery`). The BFF says, per slot (1–10), whether that code was used and when
 * (`recoveryCodes` of `GET /api/me/totp`, GUI-9.8), never the codes themselves: the grid shows one masked slot each, used ones struck through
 * with their time. The codes appear once, in the authenticator box, right after enrolment or "Generate new recovery codes". Against a BFF that
 * gives only the count (`recoveryCodesLeft`), the grid shows that many unused slots. */
import { Card } from "../../../components/ui";
import { Empty, QueryState } from "../../../kit/states";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { recoveryLeftText } from "../../../lib/mfa";
import type { TotpStatus } from "../../../api/types";
import { useTotpStatus } from "../data/queries";

/** The slots to draw: the server's, or `recoveryCodesLeft` unused ones when it lists none. */
export function recoverySlots(s: TotpStatus): { slot: number; used: boolean; usedAt: string | null }[] {
  if (s.recoveryCodes && s.recoveryCodes.length > 0) return [...s.recoveryCodes].sort((a, b) => a.slot - b.slot);
  return Array.from({ length: Math.min(s.recoveryCodesLeft, 20) }, (_, i) => ({ slot: i + 1, used: false, usedAt: null }));
}

/** The box; hidden for an identity-provider user (no codes here). */
export function RecoveryCodes() {
  const t = useT();
  const status = useTotpStatus();
  if (status.data?.reason === "identity provider") return null;
  const slots = status.data ? recoverySlots(status.data) : [];
  return (
    <Card section="security.recovery" title={t("security.recovery.title")} sub={t("security.recovery.sub")}>
      <QueryState q={status} isEmpty={() => false}>
        {status.data && !status.data.enrolled ? (
          <Empty title={t("security.recovery.none.title")}>{t("security.recovery.none.body")}</Empty>
        ) : status.data && (
          <>
            <p className="small">{recoveryLeftText(status.data.recoveryCodesLeft, t)}</p>
            <ul className="recovery-codes" aria-label={t("security.recovery.unused", { left: status.data.recoveryCodesLeft })}>
              {slots.map((c) => (
                <li key={c.slot} className={c.used ? "used" : undefined} title={c.used ? t("security.recovery.usedAt", { time: formatTime(c.usedAt) }) : t("security.recovery.notUsed")}>
                  <span className="xs muted">#{c.slot} </span><code aria-hidden>••••-••••-••••-••••</code>
                  {c.used && <span className="xs muted"> {t("security.recovery.usedAt", { time: formatTime(c.usedAt) })}</span>}
                </li>
              ))}
            </ul>
            <p className="small muted">{t("security.recovery.note")}</p>
          </>
        )}
      </QueryState>
    </Card>
  );
}
