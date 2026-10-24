"""PR-SEC-10: what a caller with a scope claim may touch, decided here because this module owns the targets (docs/adr/0005-tenant-region-authorization.md).

The claim comes from the gateway (`smo_shared.scope.request_scope`); the target's `region` and `tenant` are columns of `managed_entity`. Everything here is the one rule of
`smo_shared.scope.permits`, applied to the registry:

  - an **unscoped** caller (no claim) is never asked anything: no query is made, the answer is "yes", exactly as before this existed;
  - a managed element that is **not registered** has no region and no tenant, so a scoped caller may not touch it. A request that names an element by its reference
    (a config job, a read of its configuration) therefore gets the same 403 `SCOPE_DENIED` for an element that does not exist as for one that is out of scope: the
    answer does not tell a scoped caller which references exist;
  - an item addressed by an id the system made (an alarm, a job, a file) that is out of scope is a 404, as if it did not exist; a list leaves it out;
  - a managed object addressed by its DN (`/managed-objects/{dn}`) is the same: the tree a scoped caller sees is the tree of its own elements, so an object of another
    element is a 404 exactly as one that is not there (a 403 beside a 404 would tell it which DNs exist);
  - what has no element of its own (the vendor and CM-schema registries) is shown for what the caller's elements use (`visible_vendor_names`);
  - ownership of a job (PR-SEC-10.11) is a second, separate rule: whom a job belongs to (`owned_jobs`, `job_owned_by`), applied only to a caller with a claim.

PR-GUI-9.3 adds the **place filters** at the bottom of this file: the optional `region` and `site_cluster` query parameters every list tied to managed
elements accepts, matched against `managed_entity.region` / `.site_cluster`. They narrow; they never authorize: the caller's scope claim above still
applies (or, on a list that is internal-only and not scoped, is not asked), and a filter can only take rows away. A row tied to several elements (a
config job, an approval, a decision record) matches when ANY of its elements is in the place. The SQL is portable except for the expansion of a JSON
list of references (`json_each` on SQLite, `json_array_elements_text` on Postgres), which branches on the dialect like `alarm_query.py` does.
"""

import uuid
from collections.abc import Iterable

from fastapi import Query, Request
from sqlalchemy import exists, false, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement, Select

from smo_shared.errors import FrameworkError, framework_error
from smo_shared.scope import Scope, denied_condition, filter_statement, permits, request_scope

from .models import ManagedEntity, WriteConfigJob

__all__ = ["Scope", "request_scope", "element_permitted", "denied_refs", "require_elements", "visible_elements", "scoped_to_elements", "scope_denied",
           "visible_vendor_names", "owned_jobs", "job_owned_by",
           "RegionFilter", "SiteClusterFilter", "place_refs", "narrowed_to_place", "json_refs_in_place", "json_refs_inside_scope"]

MAX_LISTED = 10


def scope_denied(detail: str = "the caller's scope does not cover this managed element"):
    return framework_error(FrameworkError.SCOPE_DENIED, detail=detail)


def element_permitted(db: Session, scope: Scope | None, ref: str) -> bool:
    """Whether the caller's scope covers the managed element `ref`: True without a claim (no query is made); otherwise True only for a registered element whose region and tenant the scope permits. An unregistered element is not permitted, so a scoped caller cannot tell it from an out-of-scope one.
    """
    if scope is None:
        return True
    row = db.execute(select(ManagedEntity.region, ManagedEntity.tenant).where(ManagedEntity.managed_element_ref == ref)).one_or_none()     # column select: never a stale identity-map row
    return row is not None and permits(scope, row.region, row.tenant)


def denied_refs(db: Session, scope: Scope | None, refs: Iterable[str]) -> list[str]:
    """The references among `refs` that the caller may not touch (not registered, or outside its claim), in the order given, each once."""
    wanted = list(dict.fromkeys(refs))
    if scope is None or not wanted:
        return []
    known = {row.managed_element_ref: (row.region, row.tenant) for row in db.execute(
        select(ManagedEntity.managed_element_ref, ManagedEntity.region, ManagedEntity.tenant).where(ManagedEntity.managed_element_ref.in_(wanted)))}
    return [ref for ref in wanted if ref not in known or not permits(scope, *known[ref])]


def require_elements(db: Session, scope: Scope | None, refs: Iterable[str]) -> None:
    """403 `SCOPE_DENIED` naming (the first few of) the references the caller sent that it may not touch; nothing for an unscoped caller."""
    denied = denied_refs(db, scope, refs)
    if denied:
        shown = ", ".join(denied[:MAX_LISTED]) + (f" and {len(denied) - MAX_LISTED} more" if len(denied) > MAX_LISTED else "")
        raise scope_denied(f"the caller's scope does not cover: {shown}")


def visible_elements(scope: Scope | None) -> Select:
    """The references of the managed elements a caller may touch, as a subquery for `column.in_(...)`."""
    return filter_statement(select(ManagedEntity.managed_element_ref), scope, ManagedEntity.region, ManagedEntity.tenant)


def scoped_to_elements(stmt: Select, scope: Scope | None, element_column) -> Select:
    """`stmt` limited to rows whose element (`element_column`, a reference to `managed_entity`) the caller may touch; unchanged for an unscoped caller."""
    return stmt if scope is None else stmt.where(element_column.in_(visible_elements(scope)))


def visible_vendor_names(scope: Scope | None) -> Select:
    """The vendors of the managed elements a caller may touch, as a subquery for `column.in_(...)`. The vendor registry has no element, region or tenant of its own;
    a scoped caller sees a vendor's entry when one of its own elements is of that vendor (so it reads what the checks on its elements are made from, and learns nothing of
    the vendors of the rest of the network). An element with no vendor adds none."""
    return scoped_to_elements(select(ManagedEntity.vendor_name).where(ManagedEntity.vendor_name.is_not(None)), scope, ManagedEntity.managed_element_ref)


def owned_jobs(caller: str | None) -> Select:
    """PR-SEC-10.11: the ids of the configuration jobs that belong to `caller` (the invoker id the gateway vouches for), as a subquery for `column.in_(...)`: the jobs it
    made, and the jobs that undo them (a rollback, or the revert of a failed wave, is made on the original's record and carries no invoker of its own, and it belongs to whom the
    job it undoes belongs to, however many rollbacks deep). A job made by an operator or an SMO module belongs to no rApp. `None` owns nothing."""
    base = select(WriteConfigJob.job_id).where(WriteConfigJob.invoker_id == caller if caller is not None else false())
    owned = base.cte("owned_jobs", recursive=True)
    owned = owned.union_all(select(WriteConfigJob.job_id).where(WriteConfigJob.rollback_of == owned.c.job_id))
    return select(owned.c.job_id)


def job_owned_by(db: Session, job: WriteConfigJob, caller: str | None) -> bool:
    """Whether `job` belongs to `caller`: the same rule as `owned_jobs`, for one job (read by its id)."""
    seen: set[uuid.UUID] = set()
    current: WriteConfigJob | None = job
    while current is not None and current.job_id not in seen:
        if caller is not None and current.invoker_id == caller:
            return True
        seen.add(current.job_id)
        current = db.get(WriteConfigJob, current.rollback_of) if current.rollback_of else None
    return False


# ---------------------------------------------------------------- PR-GUI-9.3: the region / site cluster filters

# The two query parameters, shared so that every list documents them the same way in the OpenAPI document. Same length bound as the stored values.
RegionFilter = Query(None, min_length=1, max_length=100,
                     description="Keep the rows tied to a managed element of this region (`managed_entity.region`, ADR 0005). Narrows only: the caller's scope claim still applies.")
SiteClusterFilter = Query(None, min_length=1, max_length=100,
                          description="Keep the rows tied to a managed element of this site cluster (`managed_entity.site_cluster`). Narrows only.")


def place_refs(region: str | None, site_cluster: str | None) -> Select | None:
    """The references of the registered elements in `region` and `site_cluster` (both must match when both are given), as a subquery for
    `column.in_(...)`; `None` when neither is given (no filter). An element whose region or cluster is not set never matches a value."""
    if not region and not site_cluster:
        return None
    stmt = select(ManagedEntity.managed_element_ref)
    if region:
        stmt = stmt.where(ManagedEntity.region == region)
    if site_cluster:
        stmt = stmt.where(ManagedEntity.site_cluster == site_cluster)
    return stmt


def narrowed_to_place(stmt: Select, element_column, region: str | None, site_cluster: str | None) -> Select:
    """`stmt` limited to rows whose element (`element_column`, a managed element reference) is in the place; unchanged when no filter is given."""
    refs = place_refs(region, site_cluster)
    return stmt if refs is None else stmt.where(element_column.in_(refs))


def json_refs_in_place(db: Session, list_column, region: str | None, site_cluster: str | None) -> ColumnElement | None:
    """A condition true when the JSON list of element references in `list_column` (for example `rapp_decision_record.managed_elements`) names at
    least one element in the place; `None` when no filter is given. An empty list, or one naming only unregistered elements, never matches.

    The list is expanded in SQL as a correlated EXISTS, so the filter costs no extra round trip and the page count stays exact. The two dialects
    spell the expansion differently: Postgres `json_array_elements_text` (the columns are `JSON`, not `JSONB`), named with a derived column list
    because a set-returning function's column is otherwise named after its alias; SQLite `json_each`, whose result column is always `value`
    (SQLite does not accept a derived column list there)."""
    refs = place_refs(region, site_cluster)
    if refs is None:
        return None
    items = _json_refs(db, list_column, "place_ref")
    return exists(select(items.c.value).where(items.c.value.in_(refs)))


def _json_refs(db: Session, list_column, name: str):
    """The JSON list of references in `list_column` expanded as a table-valued function with one column, `value` (see `json_refs_in_place` for the two dialects)."""
    if db.get_bind().dialect.name == "postgresql":
        return func.json_array_elements_text(list_column).table_valued("value").render_derived(name=name)
    return func.json_each(list_column).table_valued("value")


def json_refs_inside_scope(db: Session, list_column, scope: Scope | None) -> ColumnElement | None:
    """SEC-10.12: a condition true when every element reference in the JSON list `list_column` (for example `rapp_action_approval.managed_elements`) is one the
    caller may touch, by the rule of `denied_refs` (an unregistered reference counts as outside); `None` for an unscoped caller (no filter). An empty list has nothing
    outside the scope, so it matches. Computed in SQL as a NOT EXISTS over the expanded list, so `total` and the page agree with what the caller may see."""
    if scope is None:
        return None
    items = _json_refs(db, list_column, "scope_ref")
    outside = (select(items.c.value).outerjoin(ManagedEntity, ManagedEntity.managed_element_ref == items.c.value)
               .where(ManagedEntity.managed_element_ref.is_(None) | denied_condition(scope, ManagedEntity.region, ManagedEntity.tenant)))
    return ~exists(outside)
