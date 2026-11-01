"""Distinguished names of managed object instances (TS 32.300 DN form, TS 32.158 clause 4.4.2 URI form).

A DN is a comma-separated list of relative DNs, each `ClassName=id`, outermost first: `SubNetwork=BLR,ManagedElement=1,GNBDUFunction=1`. On the wire
(TS 32.158 clause 4.4.2) the same name is a path: `{apiRoot}/ProvMnS/{version}/SubNetwork=BLR/ManagedElement=1/GNBDUFunction=1`, the leading RDNs being
the `URI-LDN-first-part` and the last one the `{className}={id}` of the TS 28.532 OpenAPI. Both forms map one to one.

What a DN may hold is narrower here than in TS 32.300, on purpose: a class name is a letter followed by letters, digits and underscores (every class of
the vendored NRM fits); an id is one or more of the RFC 3986 unreserved characters plus `:` and `@` (no `,`, `=`, `/`, `%` or space). That keeps a DN
usable unescaped in a URI path and keeps the store's subtree range query exact: every descendant of `X=1` starts with `X=1,`, and `,` sorts below every
character an id may hold, so the descendants are exactly the DNs in [`X=1,`, `X=1-`) under binary (C) collation.
"""

import re
from dataclasses import dataclass

from .errors import MnsError

CLASS_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
MOI_ID = re.compile(r"[A-Za-z0-9._~:@-]+")
SUBTREE_SEPARATOR = ","
SUBTREE_UPPER = "-"          # the character after "," in ASCII: [dn + ",", dn + "-") is every descendant of dn (see the module description)


@dataclass(frozen=True)
class Rdn:
    """One relative DN, `class_name=moi_id`."""

    class_name: str
    moi_id: str

    def __str__(self) -> str:
        return f"{self.class_name}={self.moi_id}"


def parse_rdn(text: str) -> Rdn:
    """`ClassName=id` as an `Rdn`; a malformed one is a 400 naming it."""
    class_name, sep, moi_id = text.partition("=")
    if not sep or not CLASS_NAME.fullmatch(class_name) or not MOI_ID.fullmatch(moi_id):
        raise MnsError(400, "INVALID_DN", f"{text!r} is not a relative DN of the form ClassName=id", bad_objects=[text])
    return Rdn(class_name, moi_id)


def parse_dn(dn: str) -> tuple[Rdn, ...]:
    """The RDNs of a comma-separated DN, outermost first; an empty or malformed DN is a 400."""
    if not dn:
        raise MnsError(400, "INVALID_DN", "the DN is empty", bad_objects=[dn])
    return tuple(parse_rdn(part) for part in dn.split(SUBTREE_SEPARATOR))


def dn_from_path(ldn_path: str) -> str:
    """The DN a TS 32.158 URI path names (`SubNetwork=A/ManagedElement=1`), validated; empty segments (a doubled or trailing `/`) are a 400."""
    parts = ldn_path.split("/")
    return join(parse_rdn(part) for part in parts)


def join(rdns) -> str:
    """The DN of a sequence of RDNs."""
    return SUBTREE_SEPARATOR.join(str(rdn) for rdn in rdns)


def path_of(dn: str) -> str:
    """The URI path form of a DN (`/` between the RDNs)."""
    return "/".join(str(rdn) for rdn in parse_dn(dn))


def parent_of(dn: str) -> str | None:
    """The DN one level up, or None for a top-level object."""
    head, sep, _ = dn.rpartition(SUBTREE_SEPARATOR)
    return head if sep else None


def depth_of(dn: str) -> int:
    """The number of RDNs in the DN (a top-level object has depth 1)."""
    return dn.count(SUBTREE_SEPARATOR) + 1


def is_descendant(dn: str, base: str) -> bool:
    """Whether `dn` is strictly below `base` in the containment tree."""
    return dn.startswith(base + SUBTREE_SEPARATOR)
