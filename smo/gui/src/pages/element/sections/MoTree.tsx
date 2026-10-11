/** Element detail · managed-object tree (`element.mo`): the element's containment tree from its root DN (`GET /managed-objects/{dn}`).
 * A node's children load only when it is opened (`/children?limit=50`, "more" asks for 50 more), and "Expand 3 levels" loads the selected
 * node's subtree in one call (`/subtree?depth=3`, at most 1000 nodes). Selecting a node shows it in the attributes box (`?mo=`). "Refresh
 * from element" (`POST …/managed-objects/refresh`, a walk of the element's server) is an operator's call; a viewer does not see the button. */
import { useEffect, useState } from "react";

import { ActionButton, Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { refreshPath, useChildren, useMo, useSelectedMo, useSubtree } from "../data/queries";
import { rootDn, type ManagedObject } from "../data/types";

const STEP = 50;

/** Shared tree state: open nodes, children a subtree answer already holds, the selection. */
interface TreeState { open: Set<string>; toggle: (dn: string) => void; preloaded: Map<string, ManagedObject[]>; selected: string | null; select: (dn: string) => void }

/** The tree card of element `me`. */
export function MoTree({ me }: { me: string }) {
  const t = useT();
  const root = useMo(rootDn(me));
  const [selected, select] = useSelectedMo();
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const [preloaded, setPreloaded] = useState<Map<string, ManagedObject[]>>(() => new Map());
  const [expandAt, setExpandAt] = useState<string | null>(null);
  const subtree = useSubtree(expandAt, 3);
  useEffect(() => {
    if (!subtree.data) return;
    const kids = new Map(preloaded), opened = new Set(open);
    const walk = (n: ManagedObject) => { if (n.children) { kids.set(n.dn, n.children); opened.add(n.dn); n.children.forEach(walk); } };
    walk(subtree.data.tree);
    setPreloaded(kids);
    setOpen(opened);
  }, [subtree.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const state: TreeState = {
    open, preloaded, selected, select,
    toggle: (dn) => setOpen((s) => { const n = new Set(s); if (n.has(dn)) n.delete(dn); else n.add(dn); return n; }),
  };
  return (
    <Card section="element.mo" title={t("element.mo.title")} sub={t("element.mo.sub")}
      actions={<>
        <button type="button" className="btn small" disabled={!selected || subtree.isFetching} onClick={() => { setExpandAt(selected); void (expandAt === selected && subtree.refetch()); }}>{t("element.mo.expand")}</button>
        <ActionButton label={t("element.mo.refresh")} action={{ method: "POST", path: refreshPath(me), success: t("element.mo.refreshed") }} />
      </>}>
      {subtree.data?.truncated && <p className="gap-note">{t("element.mo.truncated")}</p>}
      {root.error && (root.error.status === 404 ? <Empty title={t("element.mo.notInTree")} /> : <ErrorRetry error={root.error} onRetry={() => void root.refetch()} />)}
      {!root.data && !root.error && <Skeleton lines={5} />}
      {root.data && <ul className="mo-tree" role="tree" aria-label={t("element.mo.title")}><Node mo={root.data} depth={0} tree={state} /></ul>}
    </Card>
  );
}

/** One node and, when open, its children. */
function Node({ mo, depth, tree }: { mo: ManagedObject; depth: number; tree: TreeState }) {
  const t = useT();
  const isOpen = tree.open.has(mo.dn);
  const pre = tree.preloaded.get(mo.dn);
  const [limit, setLimit] = useState(STEP);
  const kids = useChildren(mo.dn, limit, isOpen && (!pre || limit > STEP));
  const children = kids.data?.items ?? pre;
  const total = kids.data?.total ?? pre?.length;
  const label = `${mo.class}=${mo.id}`;
  return (
    <li role="treeitem" aria-expanded={isOpen} aria-selected={tree.selected === mo.dn}>
      <div className={`mo-row${tree.selected === mo.dn ? " on" : ""}`} style={{ paddingLeft: 8 + depth * 16 }}>
        <button type="button" className="mo-toggle" aria-label={t(isOpen ? "element.mo.close" : "element.mo.open", { label })} onClick={() => tree.toggle(mo.dn)}>{isOpen ? "▾" : "▸"}</button>
        <button type="button" className="mo-label mono small" onClick={() => tree.select(mo.dn)}>{label}</button>
        {mo.source === "walk" && <span className="xs muted">{t("element.mo.walked")}</span>}
      </div>
      {isOpen && (
        <ul role="group">
          {kids.isLoading && !pre && <li className="muted small" style={{ paddingLeft: 24 + depth * 16 }}>{t("element.mo.loading")}</li>}
          {kids.error && <li style={{ paddingLeft: 24 + depth * 16 }}><ErrorRetry error={kids.error} onRetry={() => void kids.refetch()} /></li>}
          {children?.length === 0 && <li className="muted small" style={{ paddingLeft: 24 + depth * 16 }}>{t("element.mo.noChildren")}</li>}
          {children?.map((c) => <Node key={c.dn} mo={c} depth={depth + 1} tree={tree} />)}
          {children && total !== undefined && total > children.length && (
            <li style={{ paddingLeft: 24 + depth * 16 }}><button type="button" className="btn small ghost" onClick={() => setLimit(Math.max(limit, children.length) + STEP)}>{t("element.mo.more", { n: total - children.length })}</button></li>
          )}
        </ul>
      )}
    </li>
  );
}
