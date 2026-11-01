#!/usr/bin/env python3
"""A sketch of TE&IV's standards face over smo-teiv (plan task S0.13, ADR 0013): not production code, no module imports it.

D19 lets TE&IV reuse O-RAN-SC smo-teiv but expose it only through the standards. This file tries that face on the spike's queries:

- `get_moi_attributes(dn, scope, filter)`: a TS 28.532 ProvMnS getMOIAttributes read (scope BASE_ONLY / BASE_ALL / BASE_NTH_LEVEL, a filter by class
  and attribute values) answered from smo-teiv's own REST API, and its answer in the TS 28.532 form (MOIs by DN, with `objectClass` and
  `attributes`). The DN is the 3GPP DN below the SubNetwork; smo-teiv keys every entity by `urn:3gpp:dn:<DN>` (load.py), so a DN and an id map one to
  one, and the SubNetwork is kept as a sourceId. What smo-teiv cannot answer raises `NotExpressible` with the reason (the gap is in ADR 0013).
- `notifications_for(event, before)`: the TS 28.532 notifications (notifyMOICreation, notifyMOIAttributeValueChanges, notifyMOIDeletion) for one
  smo-teiv ingestion CloudEvent, given what the store held before. smo-teiv emits no change event of its own, so TE&IV, as the only writer
  (D6), would raise them on its own ingestion path once the write is visible.
- `neighbours(dn, hops)`: the neighbour read of TE&IV's extended model (plan task S3.20): the NRCellCU neighbours of a cell (hops=1, one smo-teiv
  request on the NEIGHBOR_CELL relationship) or the cells within two hops of it (hops=2: smo-teiv's scope filter cannot follow two
  associations in one path, so the facade asks for each first-hop neighbour's neighbours, all at once). ProvMnS has no neighbour read of its own: a TS 28.532 caller reads the cell's NRCellRelation objects
  (`get_moi_attributes(cell, "BASE_ALL", filter={"class": "NRCellRelation"})`), which the extension also answers.
- `R1_OPERATIONS`: how each operation of smo-teiv's API stands against the O-RAN WG10 TE&IV R1 services (the bundle's teiv.yaml lists none of
  them yet: the API is not in specs/) and against TS 28.532.

    python spikes/teiv/facade.py --teiv http://127.0.0.1:31074 BASE_ONLY "ManagedElement=gNB-0001,GNBDUFunction=1,NRCellDU=2"
"""

import argparse
import datetime
import importlib.util
import itertools
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx

_spec = importlib.util.spec_from_file_location("spike_load", Path(__file__).with_name("load.py"))
load = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load)

API = "/topology-inventory/v1"
TEIV_ID = load.TEIV_ID
# TS 28.541 class -> (smo-teiv entity type without its module, the relationship side name a child of it is found by)
TYPES = {cls: teiv.split(":", 1)[1] for cls, (teiv, _) in load.CLASS_MAP.items()}
# TS 28.541 class -> the smo-teiv domain its entities are read in (its module's or-teiv-yext:domain)
DOMAIN_OF = {cls: load.DOMAINS[teiv.split(":", 1)[0]] for cls, (teiv, _) in load.CLASS_MAP.items()}
# (parent class, child class) -> (the association a child is found by, the domain that sees the relationship carrying it)
CHILD_SCOPE = {("GNBDUFunction", "NRCellDU"): ("provided-by-oduFunction", "RAN"), ("GNBDUFunction", "NRSectorCarrier"): ("provided-by-oduFunction", "RAN"),
               ("GNBCUCPFunction", "NRCellCU"): ("provided-by-ocucpFunction", "RAN"),
               ("ManagedElement", "GNBDUFunction"): ("managed-by-managedElement", "REL_OAM_RAN"),
               ("ManagedElement", "GNBCUCPFunction"): ("managed-by-managedElement", "REL_OAM_RAN"),
               # the extension (S3.20)
               ("NRCellCU", "NRCellRelation"): ("contained-by-nrCellCu", "EXT_RAN"), ("NRCellCU", "NRFreqRelation"): ("contained-by-nrCellCu", "EXT_RAN"),
               ("NRCellCU", "CESManagementFunction"): ("contained-by-nrCellCu", "EXT_RAN"),
               ("NRCellCU", "DESManagementFunction"): ("contained-by-nrCellCu", "EXT_RAN"),
               ("NRCellCU", "DMROFunction"): ("contained-by-nrCellCu", "EXT_RAN"), ("NRCellCU", "DLBOFunction"): ("contained-by-nrCellCu", "EXT_RAN"),
               ("NRSectorCarrier", "CommonBeamformingFunction"): ("contained-by-nrSectorCarrier", "EXT_RAN"),
               ("SubNetwork", "CCOFunction"): ("contained-by-subNetwork", "EXT_OAM")}
NEIGHBOUR_SCOPE = "neighbor-of-nrCellCu"    # NRCELLCU_NEIGHBOR_CELL_NRCELLCU, written with the neighbour as aSide (model/smo-teiv-ext-ran.yang)
_ids = itertools.count(1)


class NotExpressible(Exception):
    """A ProvMnS read that smo-teiv 0.3.0 cannot answer; the message says why (a class or relationship its model does not have)."""


def _cls(dn: str) -> str:
    return dn.rsplit(",", 1)[-1].split("=", 1)[0]


def _moi(teiv_type: str, entity: dict) -> dict:
    """One smo-teiv entity as a TS 28.532 MOI: the DN (the id without `urn:3gpp:dn:`), the 3GPP class, the attributes smo-teiv holds."""
    dn = entity["id"].removeprefix(TEIV_ID)
    return {"id": dn.rsplit(",", 1)[-1].split("=", 1)[1], "objectClass": _cls(dn), "objectInstance": dn, "attributes": entity.get("attributes", {})}


def _entities(body: dict) -> list[tuple[str, dict]]:
    """(type, entity) pairs of a smo-teiv answer, single or paged."""
    blocks = body.get("items", [body])
    return [(k.split(":", 1)[1], e) for block in blocks for k, v in block.items() if ":" in k for e in v]


class TeivFacade:
    """getMOIAttributes over smo-teiv's REST API (the TE&IV module would hold the only client of it)."""

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=base_url.rstrip("/") + API, timeout=30)

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    def get_moi_attributes(self, dn: str, scope: str = "BASE_ONLY", level: int = 1, filter: dict | None = None) -> list[dict]:
        """TS 28.532 getMOIAttributes. `filter` is the spike's stand-in for the ProvMnS filter: {"class": C, "attributes": {name: value}}.

        BASE_ONLY reads the object; BASE_ALL / BASE_NTH_LEVEL with a filter class read the objects of that class below `dn` (one level of
        containment that smo-teiv has a relationship for, or a SubNetwork, whose members are found by the SubNetwork sourceId: a cell is three
        containment hops below its SubNetwork, and smo-teiv's scope filter follows one). With the extension a cell's NRCellRelation objects are
        such a read.
        """
        cls = _cls(dn)
        want = (filter or {}).get("class")
        if scope == "BASE_ONLY" or not want:
            if cls not in TYPES:
                raise NotExpressible(f"smo-teiv 0.3.0 has no class for {cls} (ADR 0013)")
            body = self._get(f"/domains/{DOMAIN_OF[cls]}/entity-types/{TYPES[cls]}/entities/{quote(TEIV_ID + dn, safe=':=,')}")
            return [_moi(t, e) for t, e in _entities(body)]
        if want not in TYPES:
            raise NotExpressible(f"smo-teiv 0.3.0 has no class for {want} (ADR 0013)")
        scopes = []
        domain = DOMAIN_OF[want]
        if (cls, want) in CHILD_SCOPE:
            association, domain = CHILD_SCOPE[(cls, want)]
            scopes.append(f"/{association}[@id='{TEIV_ID + dn}']")
        elif cls == "SubNetwork":
            scopes.append(f"/sourceIds[contains(@item,'{dn}')]")
        else:
            raise NotExpressible(f"no smo-teiv relationship from {cls} to {want} (ADR 0013)")
        for name, value in ((filter or {}).get("attributes") or {}).items():
            scopes.append(f"/attributes[@{name}={value!r}]" if isinstance(value, str) else f"/attributes[@{name}={value}]")
        return self._paged(domain, TYPES[want], ";".join(scopes))

    def _paged(self, domain: str, teiv_type: str, scope_filter: str, target_filter: str = "/attributes") -> list[dict]:
        """Every entity of `teiv_type` in `domain` that `scope_filter` selects, page by page, as MOIs."""
        out, offset = [], 0
        while True:
            body = self._get(f"/domains/{domain}/entity-types/{teiv_type}/entities", targetFilter=target_filter, scopeFilter=scope_filter,
                             offset=offset, limit=500)
            page = [_moi(t, e) for t, e in _entities(body)]
            out += page
            offset += len(page)
            if not page or offset >= body.get("totalCount", 0):
                return out

    def neighbours(self, dn: str, hops: int = 1, fan_out: bool = True) -> list[dict]:
        """The NRCellCU MOIs within `hops` (1 or 2) NEIGHBOR_CELL hops of the NRCellCU `dn`, without the cell itself. Not a ProvMnS operation: the
        TE&IV query an rApp's neighbour read is (WG10 TE&IV R1 style).

        One smo-teiv request gives the neighbours. smo-teiv's scope filter follows a single association, so the second hop is more requests: with
        `fan_out` one per first-hop neighbour, sent together (each is the one-hop query, a few milliseconds); without it one request with the
        neighbours' ids joined with `or`, which smo-teiv turns into an OR of EXISTS subqueries that Postgres evaluates row by row over the whole
        NRCellCU table (seconds at 1,000 cells; ADR 0013 section 11)."""
        if _cls(dn) != "NRCellCU" or hops not in (1, 2):
            raise NotExpressible("a neighbour read is of an NRCellCU, one or two hops (spike facade)")
        first = self._paged("EXT_RAN", "NRCellCU", f"/{NEIGHBOUR_SCOPE}[@id='{TEIV_ID + dn}']")
        if hops == 1 or not first:
            return first
        if fan_out:
            with ThreadPoolExecutor(max_workers=len(first)) as pool:
                pages = list(pool.map(lambda m: self._paged("EXT_RAN", "NRCellCU", f"/{NEIGHBOUR_SCOPE}[@id='{TEIV_ID + m['objectInstance']}']"),
                                      first))
            second = [m for page in pages for m in page]
        else:
            ids = " or ".join(f"@id='{TEIV_ID + m['objectInstance']}'" for m in first)
            second = self._paged("EXT_RAN", "NRCellCU", f"/{NEIGHBOUR_SCOPE}[{ids}]")
        seen = {m["objectInstance"] for m in first} | {dn}
        return first + [m for m in second if m["objectInstance"] not in seen and not seen.add(m["objectInstance"])]


def notifications_for(event_type: str, entity_type: str, entity: dict, before: dict | None, system_dn: str = "SubNetwork=Spike") -> list[dict]:
    """The TS 28.532 notifications one smo-teiv ingestion CloudEvent stands for, given the entity before (None: it did not exist). `merge` of a new
    entity is notifyMOICreation, of an existing one with changed attributes notifyMOIAttributeValueChanges (one entry per changed attribute,
    old and new value), `delete` is notifyMOIDeletion; anything else, or a merge that changes nothing, is no notification."""
    dn = entity["id"].removeprefix(TEIV_ID)
    base = {"href": f"{API}/{dn}", "notificationId": next(_ids), "eventTime": datetime.datetime.now(datetime.UTC).isoformat(), "systemDN": system_dn,
            "objectClass": _cls(dn), "objectInstance": dn}
    new_attrs = entity.get("attributes", {})
    if event_type.endswith(".delete") or event_type.endswith("source-entity-delete"):
        return [{**base, "notificationType": "notifyMOIDeletion", "attributeList": (before or {}).get("attributes", {})}] if before else []
    if not event_type.endswith((".merge", ".create")):
        return []
    if before is None:
        return [{**base, "notificationType": "notifyMOICreation", "attributeList": new_attrs}]
    old = before.get("attributes", {})
    changed = [{"attributeName": k, "oldValue": old.get(k), "newValue": v} for k, v in new_attrs.items() if old.get(k) != v]
    return [{**base, "notificationType": "notifyMOIAttributeValueChanges", "changedAttributes": changed}] if changed else []


# Each operation of smo-teiv 0.3.0's API (docs/offeredapis, v1.2.0), what it would be in TE&IV's standards face, and how well it maps.
# "clean": the standard operation is a thin translation; "partial": it maps but loses something, said in the note; "none": no standard counterpart
# (it stays internal to TE&IV, or is not offered).
R1_OPERATIONS = [
    ("GET /domains/{d}/entity-types/{t}/entities/{id}", "TS 28.532 getMOIAttributes BASE_ONLY; WG10 TE&IV get entity", "partial",
     "the DN maps to the id one to one, but only the attributes smo-teiv's model has come back (ADR 0013 G3)"),
    ("GET /domains/{d}/entity-types/{t}/entities?targetFilter&scopeFilter", "getMOIAttributes BASE_ALL / BASE_NTH_LEVEL with filter; TE&IV query",
     "partial", "containment scope only where a relationship exists; a SubNetwork scope only through sourceIds; filter grammar differs from TS 28.532's"),
    ("GET /domains/{d}/entity-types/{t}/entities/{id}/relationships", "TE&IV relationships of an entity", "clean",
     "WG10's own relationship view; ProvMnS has none (containment is the DN, references are attributes)"),
    ("GET /domains/{d}/relationship-types/{r}/relationships[/{id}]", "TE&IV relationships by type", "clean", "WG10 only; no ProvMnS counterpart"),
    ("GET /domains, /domains/{d}/entity-types, /domains/{d}/relationship-types", "TE&IV model discovery", "clean",
     "WG10 only; the 3GPP counterpart is the MnS registry / YANG library, not ProvMnS"),
    ("GET /domains/{d}/entities?targetFilter&scopeFilter", "TE&IV cross-type query", "partial", "no ProvMnS counterpart for a query across classes"),
    ("GET /schemas, /schemas/{n}/content", "TE&IV schema discovery", "clean", "WG10 only"),
    ("POST|DELETE /schemas, /user-defined-schemas", "none", "none", "classifiers and decorators only; stays internal (a new class needs a rebuilt schema)"),
    ("POST /classifiers, /decorators, /manage-*", "none (no 3GPP counterpart)", "none", "O-RAN-SC tagging; could back TE&IV groups, not a ProvMnS write"),
    ("/groups, /groups/{id}/members, provided-members", "none (closest: TS 28.541 SubNetwork membership)", "none", "stays internal"),
    ("(missing) createMOI / modifyMOIAttributes / deleteMOI", "TS 28.532 ProvMnS writes", "none",
     "smo-teiv writes only from its Kafka ingestion; TE&IV's ProvMnS writes (RAN NF OAM, FOCOM) must become ingestion events (ADR 0013 G6)"),
    ("(missing) notifyMOICreation / notifyMOIAttributeValueChanges / notifyMOIDeletion", "TS 28.532 notifications, NtfSubscriptionControl",
     "none", "smo-teiv emits no change event; TE&IV raises them on its write path (notifications_for)"),
]


def main(argv: list[str] | None = None) -> int:
    """`facade.py --teiv URL SCOPE DN [CLASS [ATTR=VALUE ...]]`: one getMOIAttributes through the facade, printed as JSON; `--r1` prints the
    operation table instead."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--teiv", default="http://127.0.0.1:31074")
    p.add_argument("--r1", action="store_true")
    p.add_argument("args", nargs="*")
    a = p.parse_args(argv)
    if a.r1 or not a.args:
        print(json.dumps([dict(zip(("smoTeiv", "standard", "fit", "note"), row, strict=True)) for row in R1_OPERATIONS], indent=1))
        return 0
    scope, dn, *rest = a.args
    flt = {"class": rest[0], "attributes": {k: (int(v) if v.isdigit() else v) for k, v in (x.split("=", 1) for x in rest[1:])}} if rest else None
    try:
        print(json.dumps(TeivFacade(a.teiv).get_moi_attributes(dn, scope, filter=flt), indent=1))
    except NotExpressible as why:
        print(f"not expressible: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
