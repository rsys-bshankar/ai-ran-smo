/** Flow 08 board data (RAN Analytics: producer → report → subscriber query): the subject is an analytics type, gathered from the producers,
 * subscriptions and reports; the type's producers' SME service APIs prove the SME step. */
import { useQueries } from "@tanstack/react-query";

import { smo } from "../../../api/client";
import { unwrapPage, useSmo } from "../../../api/hooks";
import type { AnalyticsProducer, AnalyticsReport, AnalyticsSubscription, SmeService } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow08 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 08 for analytics type `subjectId`. */
export function useFlow08(subjectId: string | null): FlowBoardData {
  const t = useT();
  const producers = useSmo<AnalyticsProducer[]>("/ran-analytics/producers", { limit: SUBJECT_LIMIT });
  const subs = useSmo<AnalyticsSubscription[]>("/mdaf/subscriptions", { limit: SUBJECT_LIMIT });
  const reports = useSmo<AnalyticsReport[]>("/mdaf/reports", { limit: SUBJECT_LIMIT });
  const loaded = producers.data && subs.data && reports.data;
  const types = loaded ? [...new Set([...producers.data!, ...subs.data!, ...reports.data!].map((x) => x.analyticsType))] : undefined;
  const type = choose(types, subjectId, (x) => x) ?? "";
  const producerIds = [...new Set((producers.data ?? []).filter((p) => p.analyticsType === type).map((p) => p.producerId))];
  const services = useQueries({
    queries: producerIds.map((pid) => ({
      queryKey: ["smo", `/sme/published-apis/v1/${pid}/service-apis`, {}],
      queryFn: async () => unwrapPage<SmeService[]>(await smo<unknown>(`/sme/published-apis/v1/${pid}/service-apis`)),
    })),
  });
  const names = services.flatMap((s) => s.data ?? []).map((s) => s.serviceName);
  const error = producers.error ?? subs.error ?? reports.error;
  return {
    subjects: types?.map((x) => ({ id: x, label: x })),
    subjectsError: error, retry: () => { void producers.refetch(); void subs.refetch(); void reports.refetch(); },
    selected: type ? { id: type, label: type } : undefined,
    steps: flow08(type, producers.data ?? [], subs.data ?? [], reports.data ?? [], names, t),
    empty: <>{t("flows.f08.empty")} {go("/kpis#analytics", t("flows.registerProducer"))}</>,
    actions: {
      producer: go("/kpis#analytics", t("flows.registerProducer")),
      sme: go("/data#sme", t("flows.f08.sme")),
      subscribe: <ActionButton label={t("flows.f08.subscribe")} action={{ method: "POST", path: "/mdaf/subscriptions", query: { analytics_type: type, requested_by: "smo-gui" }, success: t("flows.f08.subscribed") }} />,
      publish: go("/kpis#analytics", t("flows.f08.publish")),
    },
  };
}
