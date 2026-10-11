/** Aggregates DME types, jobs and offers into the Data & Exposure flow view: producers → data types → consumers, each column at most `max` rows
 * (the biggest by job count, the rest folded into "+N other", SCALE.md "Data & Exposure"). A band's weight is a job count. A producer's band to a
 * type carries that type's jobs; a type several producers serve counts for each of them, because DME does not record which producer serves a
 * job. Pure: no React, unit-tested directly; its two words ("+N other", "(no producer)") come from the catalog in the language of `t`. */
import type { DataJob, DataOffer, DmeType } from "../../../api/types";
import { translate, type Params, type TKey, type Translator } from "../../../i18n";

/** One row of a column; `folded` > 0 marks the "+N other" row and says how many it folds. */
export interface FlowNode { key: string; label: string; value: number; folded: number; offers?: number }
/** One band between two rows of adjacent columns. */
export interface FlowLink { from: string; to: string; value: number }
/** The three columns and the two sets of bands. */
export interface Flow { producers: FlowNode[]; types: FlowNode[]; consumers: FlowNode[]; left: FlowLink[]; right: FlowLink[] }

const OTHER = "\u0000other";
const NO_PRODUCER = "\u0000none";

/** English, for a caller that names no language (the unit tests). */
const ENGLISH: Translator = Object.assign((key: TKey, params?: Params) => translate("en", key, params), { language: "en" as const });

/** Keeps the `max` biggest of `counts` (the rest folded into one "other" row when there are more than `max`) and maps every key onto its row. */
function fold(counts: Map<string, number>, label: (k: string) => string, max: number, t: Translator): { nodes: FlowNode[]; keyOf: (k: string) => string } {
  const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1] || label(a[0]).localeCompare(label(b[0])));
  const keep = sorted.length > max ? sorted.slice(0, max - 1) : sorted;
  const kept = new Set(keep.map(([k]) => k));
  const nodes: FlowNode[] = keep.map(([k, v]) => ({ key: k, label: label(k), value: v, folded: 0 }));
  const rest = sorted.filter(([k]) => !kept.has(k));
  if (rest.length) nodes.push({ key: OTHER, label: t("data.flow.other", { n: rest.length }), value: rest.reduce((s, [, v]) => s + v, 0), folded: rest.length });
  return { nodes, keyOf: (k) => (kept.has(k) ? k : OTHER) };
}

/** Adds `value` to the band `from` → `to`. */
function bump(links: Map<string, FlowLink>, from: string, to: string, value: number) {
  const id = `${from}\u0001${to}`;
  const l = links.get(id);
  if (l) l.value += value; else links.set(id, { from, to, value });
}

/** The flow, its labels in the language of `t`. */
export function buildFlow(types: DmeType[], jobs: DataJob[], offers: DataOffer[], max = 8, t: Translator = ENGLISH): Flow {
  const typeName = new Map(types.map((t) => [t.dmeTypeId, t.typeName]));
  const jobsByType = new Map<string, number>(types.map((t) => [t.dmeTypeId, 0]));
  const jobsByConsumer = new Map<string, number>();
  for (const j of jobs) {
    jobsByType.set(j.dmeTypeId, (jobsByType.get(j.dmeTypeId) ?? 0) + 1);
    jobsByConsumer.set(j.consumerId, (jobsByConsumer.get(j.consumerId) ?? 0) + 1);
  }
  const offersByType = new Map<string, number>();
  for (const o of offers) offersByType.set(o.dmeTypeId, (offersByType.get(o.dmeTypeId) ?? 0) + 1);

  const producersOf = (typeId: string) => {
    const ids = types.find((t) => t.dmeTypeId === typeId)?.producerIds ?? [];
    return ids.length ? ids : [NO_PRODUCER];
  };
  const jobsByProducer = new Map<string, number>();
  for (const [typeId, n] of jobsByType) for (const p of producersOf(typeId)) jobsByProducer.set(p, (jobsByProducer.get(p) ?? 0) + n);

  const T = fold(jobsByType, (k) => typeName.get(k) ?? k.slice(0, 8), max, t);
  const P = fold(jobsByProducer, (k) => (k === NO_PRODUCER ? t("data.flow.noProducer") : k), max, t);
  const C = fold(jobsByConsumer, (k) => k, max, t);
  for (const n of T.nodes) {
    n.offers = n.key === OTHER
      ? [...offersByType.entries()].filter(([k]) => T.keyOf(k) === OTHER).reduce((s, [, v]) => s + v, 0)
      : offersByType.get(n.key) ?? 0;
  }

  const left = new Map<string, FlowLink>();
  for (const [typeId, n] of jobsByType) for (const p of producersOf(typeId)) if (n) bump(left, P.keyOf(p), T.keyOf(typeId), n);
  const right = new Map<string, FlowLink>();
  for (const j of jobs) bump(right, T.keyOf(j.dmeTypeId), C.keyOf(j.consumerId), 1);
  return { producers: P.nodes, types: T.nodes, consumers: C.nodes, left: [...left.values()], right: [...right.values()] };
}
