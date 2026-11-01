#!/usr/bin/env python3
"""Checks that TE&IV's model extension (teiv/model/smo-teiv-ext-*.yang) uses only the extension points the O-RAN TE&IV data model allows (plan task S3.24).

    python teiv/model/check_extension.py                                  # the extension modules alone
    python teiv/model/check_extension.py --models <smo-teiv>/teiv/src/main/resources/models   # and role names against smo-teiv's own modules

The rules are O-RAN.WG10.TS.TE&IV-DM.0-R005-v06.00, Annex A (o-ran-smo-teiv-common-yang-types and -extensions). The data model is extended by new modules
(a new domain with its own entity types and biDirectionalTopologyRelationships, as Annex B to G are), by decorators (an augmentation of the `decorators`
container, from a consumer's module, Annex E) and by classifiers (identities derived from `classifier`). It says the `sourceIds` groupings, the
`decorators` container and the `classifiers` leaf-list "MUST NOT be augmented or deviated in any way". So TE&IV's extension, a domain module:

  E1  has no `augment` and no `deviation` at all (it adds types; it changes none of the data model's or smo-teiv's);
  E2  is not in the data model's namespace (`urn:o-ran:smo-teiv-...`) and does not take a data model module's name (`o-ran-smo-teiv-...`);
  E3  declares its own domain (`or-teiv-yext:domain`), not one of the data model's;
  E4  gives every entity type (a top-level `list`) `uses ...:Top_Grp_Type` ("All types MUST use this grouping") and the entity mapping grouping;
  E5  declares each biDirectionalTopologyRelationship directly under `module`, once per name, with `Top_Grp_Type` and the relationship mapping grouping,
      and exactly one `aSide` and one `bSide`, each on a `leaf` or `leaf-list` of type `instance-identifier`;
  E6  uses `or-teiv-yext:label` only inside `revision`, as x.y.z;
  E7  gives every role a name unique on its type, across the extension and, with --models, smo-teiv's own modules.

The data model itself (DM v06's YANG) is not in specs/ yet: its source is the O-RAN repository gitlab.o-ran.org/wg10/teiv-dm, which this environment cannot
reach. Until it is, smo-teiv 0.3.0's modules (its realisation of the DM, R004 v02.00 / R005 v04.00) stand in for it in E7. The parse is pyang's own
(statements only: imports need not resolve). Exit 1 when a rule is broken, with one line per finding.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from pyang import context, repository, yang_parser

HERE = Path(__file__).resolve().parent
DM_DOMAINS = {"RAN", "PHYSICAL", "REL_PHYSICAL_RAN", "CLOUD", "REL_CLOUD_RAN", "REL_RAN_CLOUD", "OAM", "EQUIPMENT", "REL_EQUIPMENT_RAN", "REL_OAM_RAN",
              "REL_OAM_CLOUD"}          # DM v06 Annex B to G, and the smo-teiv 0.3.0 modules of the same family
LABEL = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def parse(path: Path):
    """The module statement of the YANG file `path`, parsed by pyang (no import resolution); ValueError when it does not parse."""
    ctx = context.Context(repository.FileRepository(str(path.parent)))
    stmt = yang_parser.YangParser().parse(ctx, str(path), path.read_text(encoding="utf-8"))
    if stmt is None:
        raise ValueError(f"{path.name}: does not parse: {[e[1] for e in ctx.errors]}")
    return stmt


def _kw(stmt) -> str:
    """A statement's keyword without its prefix (`or-teiv-yext:aSide` -> `aSide`)."""
    return stmt.keyword[1] if isinstance(stmt.keyword, tuple) else stmt.keyword


def _walk(stmt):
    """`stmt` and every statement below it."""
    yield stmt
    for sub in stmt.substmts:
        yield from _walk(sub)


def _uses(stmt) -> set[str]:
    """The grouping names (prefix dropped) `stmt` uses directly."""
    return {s.arg.split(":")[-1] for s in stmt.substmts if s.keyword == "uses"}


def roles(module) -> list[tuple[str, str, str]]:
    """(type, role, relationship) for both sides of every relationship of `module`; the type keeps no prefix."""
    out = []
    for rel in (s for s in module.substmts if _kw(s) == "biDirectionalTopologyRelationship"):
        for leaf in (s for s in rel.substmts if s.keyword in ("leaf", "leaf-list")):
            for side in (s for s in leaf.substmts if _kw(s) in ("aSide", "bSide")):
                out.append((side.arg.split(":")[-1], leaf.arg, rel.arg))
    return out


def check(module) -> list[str]:
    """The findings of rules E1 to E6 on one extension module."""
    name, found = module.arg, []
    for s in _walk(module):
        if s.keyword in ("augment", "deviation"):
            found.append(f"{name}: E1 {s.keyword} {s.arg} (line {s.pos.line}): the extension adds types, it augments or deviates nothing")
    namespace = next((s.arg for s in module.substmts if s.keyword == "namespace"), "")
    if name.startswith("o-ran-smo-teiv-") or namespace.startswith("urn:o-ran:smo-teiv"):
        found.append(f"{name}: E2 the module takes the data model's name or namespace ({namespace})")
    domains = [s.arg for s in module.substmts if _kw(s) == "domain"]
    if len(domains) != 1:
        found.append(f"{name}: E3 {len(domains)} domain statements, one expected")
    elif domains[0] in DM_DOMAINS:
        found.append(f"{name}: E3 domain {domains[0]} is the data model's")
    for entity in (s for s in module.substmts if s.keyword == "list"):
        missing = {"Top_Grp_Type", "Origin_Entity_Mapping_Grp"} - _uses(entity)
        if missing:
            found.append(f"{name}: E4 entity type {entity.arg} does not use {sorted(missing)}")
    seen: set[str] = set()
    for s in _walk(module):
        if _kw(s) == "biDirectionalTopologyRelationship" and s.parent is not module:
            found.append(f"{name}: E5 relationship {s.arg} is not a substatement of module")
    for rel in (s for s in module.substmts if _kw(s) == "biDirectionalTopologyRelationship"):
        if rel.arg in seen:
            found.append(f"{name}: E5 relationship {rel.arg} declared twice")
        seen.add(rel.arg)
        missing = {"Top_Grp_Type", "Origin_Relationship_Mapping_Grp"} - _uses(rel)
        if missing:
            found.append(f"{name}: E5 relationship {rel.arg} does not use {sorted(missing)}")
        for side in ("aSide", "bSide"):
            leaves = [leaf for leaf in rel.substmts if leaf.keyword in ("leaf", "leaf-list") and any(_kw(x) == side for x in leaf.substmts)]
            if len(leaves) != 1:
                found.append(f"{name}: E5 relationship {rel.arg} has {len(leaves)} {side}, one expected")
            for leaf in leaves:
                kind = next((x.arg for x in leaf.substmts if x.keyword == "type"), None)
                if kind != "instance-identifier":
                    found.append(f"{name}: E5 {rel.arg} {side} {leaf.arg} is of type {kind}, not instance-identifier")
    for s in _walk(module):
        if _kw(s) == "label" and (s.parent.keyword != "revision" or not LABEL.fullmatch(s.arg or "")):
            found.append(f"{name}: E6 label {s.arg!r} (line {s.pos.line}) not a revision's x.y.z")
    return found


def check_roles(extension: Iterable, base: Iterable) -> list[str]:
    """Rule E7: a role name used twice on one type, across the extension modules and the base modules."""
    found, owner = [], {}
    for module, is_extension in [(m, False) for m in base] + [(m, True) for m in extension]:
        for etype, role, rel in roles(module):
            key = (etype, role)
            if key in owner and (is_extension or owner[key][1]):
                found.append(f"{module.arg}: E7 role {role} on {etype} ({rel}) is already {owner[key][0]}'s")
            owner.setdefault(key, (f"{module.arg} {rel}", is_extension))
    return found


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring; 0 when every rule holds."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="*", type=Path, help="extension modules (default: teiv/model/smo-teiv-ext-*.yang)")
    p.add_argument("--models", type=Path, help="a directory of the data model's (or smo-teiv's) modules, for the role check E7")
    a = p.parse_args(argv)
    files = a.files or sorted(HERE.glob("smo-teiv-ext-*.yang"))
    extension = [parse(f) for f in files]
    base = [parse(f) for f in sorted(a.models.glob("o-ran-smo-teiv-*.yang"))] if a.models else []
    found = [x for m in extension for x in check(m)] + check_roles(extension, base)
    for line in found:
        print(line)
    types = sum(1 for m in extension for s in m.substmts if s.keyword == "list")
    rels = sum(1 for m in extension for s in m.substmts if _kw(s) == "biDirectionalTopologyRelationship")
    print(f"check_extension: {len(extension)} modules, {types} entity types, {rels} relationships, {len(base)} base modules: "
          + ("OK" if not found else f"{len(found)} findings"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
