"""getMOIAttributes over smo-teiv (plan tasks S3.19, S3.20; ADR 0013 G1, G8, section 7): what TE&IV reads from smo-teiv for one TS 28.532 read, and the
TS 28.532 resources it makes of the answers.

How a read becomes smo-teiv requests (all sent together, `ThreadPoolExecutor`, on one client):

  the base            one entity read, by its class's type and domain and `urn:3gpp:dn:<DN>` (model.py); 404 MOI_NOT_FOUND when smo-teiv has none
  below the base      per class that can lie in the scope's depth range below the base's class (model.descendants), one paged list of that type with
                      `scopeFilter /sourceIds[contains(@item,'urn:3gpp:dn:<base DN>,')]`: every object's only sourceId is its own id, so this is the
                      subtree, whatever the containment path (a cell three levels below its SubNetwork is one request, as a relation one level below
                      its cell is); the depth range is applied to the DNs that come back
  the filter (G8)     the TS 28.532 filter is evaluated by TE&IV on every candidate (smo_shared.mns.scope.matches), so the answer is exactly what the
                      filter selects. Before that, what smo-teiv can evaluate itself is sent down so fewer candidates come back: from the top-level
                      `&&` terms, `@.objectClass == 'C'` narrows the classes asked for, and `@.attributes.<a> == <literal>` becomes
                      `/attributes[@<smo-teiv name>=<literal>]` for a native attribute or `/decorators[@smo-teiv-attrs:<a>='<text>']` for a
                      decorator held as itself (attrs.pushable). Any other term (`||`, `!`, a range, a path into a structure) is TE&IV's alone.
  references          per reference attribute of the classes in the answer (model.REFERENCES), one list of that relationship type with the same
                      sourceIds condition, mapped back: NRCellDU.nRSectorCarrierRef from NRCELLDU_USES_NRSECTORCARRIER, NRCellRelation's
                      nRFreqRelationRef from NRCELLRELATION_USES_NRFREQRELATION, and adjacentNRCellRef from NEIGHBOR_CELL reversed: its aSide is the
                      relation's adjacent cell and its third sourceId the relation (S3.20, ADR 0013 section 11.5 b5)

What is refused, with the reason, rather than answered wrongly or slowly:

  422 CLASS_NOT_SUPPORTED   the base's class is not one TE&IV holds (model.CLASSES)
  422 INVALID_DN            a DN with a character smo-teiv's filter grammar cannot carry (`'`, `[`, `]`, `;`): it could not be asked for exactly
  422 NOT_EXPRESSIBLE       the scope selects more than TEIV_MAX_SCOPE_OBJECTS candidates (default 20000) before TE&IV's own filtering: the caller
                            narrows the scope or filters by class or by an attribute smo-teiv holds
  503 TEIV_UNAVAILABLE      smo-teiv did not answer (client.py)

Neighbours and two hops (S3.20, the TS 28.541 forms). A cell's neighbours are its NRCellRelation objects with their adjacentNRCellRef: a
BASE_NTH_LEVEL 1 read of the NRCellCU filtered to NRCellRelation, three smo-teiv requests at once (the relations, their NEIGHBOR_CELL edges, their
NRFreqRelation edges). Two hops are the same read of each adjacent cell, sent together by the caller (the fan-out of section 11.5 b6): smo-teiv's scope
filter follows one association, and its OR of ids is slow (c3).
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from smo_shared.mns.dn import depth_of, parse_dn
from smo_shared.mns.errors import MnsError
from smo_shared.mns.scope import Scope, matches

from . import attrs, model
from .client import SmoTeivClient

ENTITY_FIELDS = "/attributes;/decorators;/sourceIds"
SMALL_FILL = 32                 # up to this many objects, their references are read object by object (exactly) rather than for the whole subtree
UNSAFE_IN_FILTER = set("'[];")
DISTINCT_VALUE = 4              # an RDN value this long is taken to tell its object apart (needle)


def max_scope_objects() -> int:
    """TEIV_MAX_SCOPE_OBJECTS: the most candidates one read may make TE&IV fetch (default 20000)."""
    try:
        return max(1, int(os.environ.get("TEIV_MAX_SCOPE_OBJECTS", "") or 20000))
    except ValueError:
        return 20000


def version(resource: dict) -> str:
    """The object version of a resource (S3.21, G7): a digest of its attributes in TS 28.541 form. smo-teiv's `lastModified` does not change when a
    decorator does, so it cannot be the version; the content can. Every change of an attribute TE&IV shows changes it."""
    text = json.dumps(resource.get("attributes", {}), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def resource_of(cls: model.TeivClass, entity: dict) -> dict:
    """One smo-teiv entity of `cls` as a TS 28.532 Resource: the DN from the id, the native attributes under their TS 28.541 names (booleans back from
    Boolean_Type), the decorators of TE&IV's schema decoded; attributes TS 28.541 does not have are left out."""
    dn = model.dn_of(entity["id"])
    out: dict = {}
    for name, value in (entity.get("attributes") or {}).items():
        mapped = model.from_teiv(cls, name, value) if value is not None else None      # a column with no value is an attribute not set
        if mapped is not None:
            out[mapped[0]] = mapped[1]
    prefix = attrs.MODULE + ":"
    for name, value in (entity.get("decorators") or {}).items():
        if name.startswith(prefix):
            out[name[len(prefix):]] = attrs.decode(value)
    return {"id": dn.rsplit(",", 1)[-1].split("=", 1)[1], "objectClass": cls.name, "objectInstance": dn, "attributes": out}


def _entities(body: dict, cls: model.TeivClass) -> list[dict]:
    """The entities of `cls` in a smo-teiv answer, single or paged."""
    blocks = body.get("items", [body])
    return [e for block in blocks for e in block.get(cls.qualified, [])]


def _safe_dn(dn: str) -> str:
    """`dn`, parsed (400 INVALID_DN otherwise), when smo-teiv's filter grammar can carry it; 422 INVALID_DN when it cannot."""
    parse_dn(dn)
    if UNSAFE_IN_FILTER & set(dn):
        raise MnsError(422, "INVALID_DN", "TE&IV cannot ask its store for a DN with ', [, ] or ;", bad_objects=[dn])
    return dn


@dataclass(frozen=True)
class Pushdown:
    """What of a filter smo-teiv can evaluate: the classes it allows (None: any) and the attribute equalities (TS name -> literal)."""

    classes: frozenset[str] | None
    equal: dict

    @staticmethod
    def of(tree) -> "Pushdown":
        """The pushdown of a parsed filter (smo_shared.mns.scope.parse_filter): the top-level `&&` terms that are class or attribute equalities."""
        terms = []

        def flatten(node):
            if node and node[0] == "and":
                for part in node[1:]:
                    flatten(part)
            elif node:
                terms.append(node)
        flatten(tree)
        classes, equal = None, {}
        for term in terms:
            if term[0] != "cmp" or term[1] != "==" or term[2][0] != "path" or term[3][0] != "literal":
                continue
            path, literal = term[2][1], term[3][1]
            if path == ["objectClass"] and isinstance(literal, str):
                classes = (classes or frozenset({literal})) & {literal}
            elif len(path) == 2 and path[0] == "attributes" and isinstance(literal, (str, int, bool)) and not isinstance(literal, float):
                equal[path[1]] = literal
        return Pushdown(classes, equal)

    def scope_filter(self, cls: model.TeivClass) -> list[str]:
        """The smo-teiv scopeFilter terms for `cls`: one per equality on an attribute it can compare (module description)."""
        out = []
        for name, literal in sorted(self.equal.items()):
            if name in cls.native:
                stored = model.to_teiv(cls, name, literal)
                if isinstance(stored, str):
                    if UNSAFE_IN_FILTER & set(stored):
                        continue
                    out.append(f"/attributes[@{cls.native[name][0]}='{stored}']")
                elif not isinstance(stored, bool):
                    out.append(f"/attributes[@{cls.native[name][0]}={stored}]")
            elif attrs.pushable(literal) and not UNSAFE_IN_FILTER & set(literal):
                out.append(f"/decorators[@{attrs.key(name)}='{literal}']")
        return out


class TeivReader:
    """The reads of the module description, on one SmoTeivClient."""

    def __init__(self, client: SmoTeivClient, workers: int = 16):
        """`workers` bounds the requests one read sends at once."""
        self.client = client
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="teiv-read")

    def close(self) -> None:
        """Stops the pool and closes the client."""
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.client.close()

    @staticmethod
    def teiv_class(dn: str) -> model.TeivClass:
        """The class TE&IV holds for `dn`'s last RDN; 422 CLASS_NOT_SUPPORTED otherwise."""
        name = model.class_of(dn)
        if name not in model.CLASSES:
            raise MnsError(422, "CLASS_NOT_SUPPORTED", f"TE&IV holds no {name} (its classes: {', '.join(sorted(model.CLASSES))})",
                           bad_objects=[name])
        return model.CLASSES[name]

    def base(self, dn: str) -> dict | None:
        """The resource at `dn` (None when smo-teiv has no such object), with its references."""
        cls = self.teiv_class(_safe_dn(dn))
        body = self.client.get_entity(cls.domain, cls.entity_type, model.teiv_id(dn))
        found = _entities(body, cls) if body else []
        if not found:
            return None
        resource = resource_of(cls, found[0])
        self._fill_references([resource], dn, exact=True)
        return resource

    def _candidates(self, cls: model.TeivClass, base_dn: str, push: Pushdown, fields: str = ENTITY_FIELDS) -> list[dict]:
        """The objects of `cls` below `base_dn` that the pushdown leaves (every page), with the entity `fields` (smo-teiv's targetFilter)."""
        terms = [needle(base_dn, below=True)] + push.scope_filter(cls)
        inside = base_dn + ","
        path = f"/domains/{cls.domain}/entity-types/{cls.entity_type}/entities"
        out, limit = [], max_scope_objects()
        for body in self.client.pages(path, {"targetFilter": fields, "scopeFilter": ";".join(terms)}):
            if body.get("totalCount", 0) > limit:
                raise MnsError(422, "NOT_EXPRESSIBLE", f"the scope selects {body['totalCount']} {cls.name} objects, more than TE&IV fetches for one "
                               f"read ({limit}): narrow the scope, or filter by class or by an attribute TE&IV's store holds",
                               bad_query_params=["scope", "filter"])
            out += [r for r in (resource_of(cls, e) for e in _entities(body, cls)) if r["objectInstance"].startswith(inside)]
        return out

    def subtree(self, base_dn: str, scope: Scope, tree, selected_attributes: set[str] | None = None) -> list[dict]:
        """The resources the scope selects from the base at `base_dn` that pass the filter `tree`, in DN order (the base first, when selected). 404
        MOI_NOT_FOUND when the base does not exist. `selected_attributes`, when given, are the attributes the caller selected: a reference
        attribute neither selected nor in the filter is not read (the objects below the base come back without it, as the selection would leave
        them)."""
        base_cls = self.teiv_class(_safe_dn(base_dn))
        base_depth = depth_of(base_dn)
        lowest, highest = scope.depth_range(base_depth)
        push = Pushdown.of(tree)
        wanted = [c for c in model.descendants(base_cls.name) if push.classes is None or c in push.classes]
        if highest is not None:
            wanted = [c for c in wanted if lowest - base_depth <= _max_depth_below(base_cls.name, c) and highest > base_depth]
        # the references of the classes asked for: read together with the candidates when the base's subtree is small (anything but a SubNetwork),
        # else after the filter, for the objects it kept (a SubNetwork-wide list of every cell's NRSectorCarrier edge is slower than the read itself)
        in_filter = _paths_of(tree)
        ref_kinds = [(k, ref) for k, ref in model.REFERENCES.items() if k[0] in wanted
                     and (selected_attributes is None or k[1] in selected_attributes or k[1] in in_filter)]
        filter_needs_refs = any(attribute in in_filter for (_cls, attribute), _ref in ref_kinds)
        early = ref_kinds and (base_cls.name != "SubNetwork" or filter_needs_refs)
        ref_jobs = self._reference_jobs(ref_kinds, needle(base_dn, below=True)) if early else {}
        # nothing selected or filtered on is a stored attribute (a neighbour read selects adjacentNRCellRef only): the ids alone
        refs_only = selected_attributes is not None and {a for _cls, a in model.REFERENCES} >= (selected_attributes | in_filter)
        fields = "/sourceIds" if refs_only else ENTITY_FIELDS
        futures = [self.pool.submit(self._candidates, model.CLASSES[c], base_dn, push, fields) for c in wanted]
        base_future = self.pool.submit(self.base, base_dn) if lowest == base_depth else None
        found = []
        for future in futures:
            for item in future.result():
                depth = depth_of(item["objectInstance"])
                if depth > base_depth and depth >= lowest and (highest is None or depth <= highest):
                    found.append(item)
        base = base_future.result() if base_future is not None else None
        if base is None and (base_future is not None or not found) and self.base(base_dn) is None:
            raise MnsError(404, "MOI_NOT_FOUND", f"{base_dn} does not exist")
        if early:
            self._apply_references(found, ref_jobs)
            selected = [r for r in found if matches(tree, r)]
        else:
            selected = [r for r in found if matches(tree, r)]
            self._fill_references(selected, base_dn, exact=False, kinds=ref_kinds)
        if base is not None and matches(tree, base):
            selected.append(base)
        return sorted(selected, key=lambda r: r["objectInstance"])

    def _reference_jobs(self, kinds, condition: str) -> dict:
        """The relationship reads of `kinds` ((class, attribute), Reference) whose sourceIds meet `condition` (needle), started."""
        return {k: self.pool.submit(self._relationships, ref, condition) for k, ref in kinds}

    def _fill_references(self, resources: list[dict], prefix_dn: str, exact: bool, kinds=None) -> None:
        """Sets the reference attributes of `resources` from their relationships (module description): one exact read per object and kind for a few
        objects (SMALL_FILL), else one read per kind of everything below `prefix_dn` (`exact`: the object at `prefix_dn` alone); `kinds`: the
        references to read (default every one of the resources' classes)."""
        kinds = [(k, ref) for k, ref in (kinds if kinds is not None else model.REFERENCES.items()) if any(r["objectClass"] == k[0] for r in resources)]
        if not kinds:
            return
        if exact or len(resources) > SMALL_FILL:
            self._apply_references(resources, self._reference_jobs(kinds, needle(prefix_dn, below=not exact)))
            return
        jobs = [(r, self._reference_jobs([(k, ref) for k, ref in kinds if k[0] == r["objectClass"]], needle(r["objectInstance"], below=False)))
                for r in resources]
        for resource, job in jobs:
            self._apply_references([resource], job)

    @staticmethod
    def _apply_references(resources: list[dict], jobs: dict) -> None:
        """Maps the relationships the `jobs` read onto the reference attributes of `resources`."""
        by_dn = {r["objectInstance"]: r for r in resources}
        for (cls_name, attribute), job in jobs.items():
            ref = model.REFERENCES[(cls_name, attribute)]
            values: dict[str, list[str]] = {}
            for rel in job.result():
                if ref.owner_source is not None:
                    sources = rel.get("sourceIds") or []
                    if len(sources) <= ref.owner_source:
                        continue
                    owner, target = sources[ref.owner_source], rel["aSide"]
                else:
                    owner, target = (rel["aSide"], rel["bSide"]) if ref.own_side == "aSide" else (rel["bSide"], rel["aSide"])
                try:
                    values.setdefault(model.dn_of(owner), []).append(model.dn_of(target))
                except ValueError:
                    continue
            for dn, targets in values.items():
                item = by_dn.get(dn)
                if item is not None and item["objectClass"] == cls_name:
                    item["attributes"][attribute] = sorted(targets) if ref.many else targets[0]

    def _relationships(self, ref: model.Reference, condition: str) -> list[dict]:
        """Every relationship of `ref`'s type whose sourceIds meet `condition` (a scopeFilter, needle)."""
        path = f"/domains/{ref.domain}/relationship-types/{ref.relationship_type}/relationships"
        out = []
        for body in self.client.pages(path, {"targetFilter": "/sourceIds", "scopeFilter": condition}):
            out += [r for block in body.get("items", []) for r in block.get(ref.relationship, [])]
        return out


def needle(dn: str, below: bool) -> str:
    """The smo-teiv scopeFilter a sourceId of `dn`'s object meets (`below`: of an object below `dn`'s). smo-teiv compares
    `"CD_sourceIds"::text LIKE '%<text>%'`, through a trigram index, and the trigrams every id shares (`urn:3gpp:dn:SubNetwork=`, `ManagedElement=`,
    `GNBCUCPFunction=1,`) make a long text slow (a cell's relations: 5.5 ms for the whole DN's tail, 2.5 ms for the two terms below). So the
    condition is the AND (`;`) of at most two short terms: the last RDN's value alone when it is distinctive (DISTINCT_VALUE characters or more),
    else the last RDN whole and the longest value of the RDNs above it (the deepest of them, on a tie). That asks for a superset; the answer is kept exact by TE&IV: the candidates
    are filtered by DN (`_candidates`), and a reference is set only on an object of the answer (`_apply_references`)."""
    rdns = dn.split(",")
    end = "," if below else ""
    value = rdns[-1].split("=", 1)[1]
    if len(rdns) == 1:
        terms = [model.teiv_id(dn) + end]
    elif len(value) >= DISTINCT_VALUE:
        terms = [f"={value}{end}"]
    else:
        above = max((r.split("=", 1)[1] for r in reversed(rdns[:-1])), key=len)       # the deepest of the longest
        terms = [f"={above},", rdns[-1] + end]
    return ";".join(f"/sourceIds[contains(@item,'{t}')]" for t in terms)


def _paths_of(tree) -> set[str]:
    """Every attribute name a parsed filter's paths name (`@.attributes.<name>...`)."""
    out: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, tuple):
            if node and node[0] == "path" and len(node[1]) >= 2 and node[1][0] == "attributes":
                out.add(node[1][1])
            for part in node[1:]:
                walk(part)
    walk(tree)
    return out


def _max_depth_below(ancestor: str, cls: str) -> int:
    """How many levels below an `ancestor` object an object of class `cls` can lie (the longest containment path; CCOFunction under its SubNetwork)."""
    best = 0

    def walk(current: str, depth: int) -> None:
        nonlocal best
        for child in model.children(current):
            if child == cls:
                best = max(best, depth + 1)
            if depth < 8:
                walk(child, depth + 1)
    walk(ancestor, 0)
    return best
