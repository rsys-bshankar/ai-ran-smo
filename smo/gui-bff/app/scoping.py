"""A console user's region and tenant scope (GUI-5): the claim kept on the user, and the headers that carry it to the SMO.

A user may carry a scope claim, `{"regions": [...], "tenants": [...]}`, set by an admin on a local account or read from the identity provider's token at each sign-in
(`GUI_OIDC_SCOPE_CLAIM`). The BFF does not decide what is in scope: it sends the claim with every call it makes for the user, in `X-R1-Acting-User-Scope` beside
`X-R1-Acting-User`, and the module that owns the data narrows its answer exactly as it does for a scoped rApp (smo_shared/scope.py `scope_of`; R1 Termination forwards
the header only from an `internal` caller, which the BFF is). A user without a claim is unscoped, as every user was before. A module that has nothing to match a claim
against (rApp Management, AIMgF, MLMR, DME's types) answers as it does for a scoped rApp: unnarrowed (docs/adr/0005-tenant-region-authorization.md).

The rules of a claim are smo_shared/scope.py's (this image does not install smo_shared, so they are repeated here and `tests/test_scope_claims.py` holds them to the same
cases): the two axes, 1 to 100 values each, the value pattern, no duplicates, no other key. What the BFF itself computes for many users at once (the summary counts,
the event stream, the search, a background export) is keyed by the claim, so one user's counts never reach another with a different claim.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

ACTING_USER_HEADER = "X-R1-Acting-User"
ACTING_USER_SCOPE_HEADER = "X-R1-Acting-User-Scope"
AXES = ("regions", "tenants")
MAX_VALUES = 100
VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")
SHARED_PERSON = "smo-gui:shared-read"        # who the BFF's shared reads (counts, search) act for: no single user, only the claim matters


def parse_claim(claim: Any) -> dict[str, list[str]] | None:
    """A claim as an admin or a token gives it, checked and normalised (sorted values, only the axes that restrict), or `None` for no claim (`None` or `{}`).
    `ValueError` with a fixed message (never the input) for one that is not valid."""
    if claim is None:
        return None
    if not isinstance(claim, Mapping):
        raise ValueError("the scope must be an object with regions and/or tenants")
    if set(claim) - set(AXES):
        raise ValueError("the scope may only name regions and tenants")
    out: dict[str, list[str]] = {}
    for axis in AXES:
        values = claim.get(axis)
        if values is None:
            continue
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
            raise ValueError(f"{axis} must be a list of 1 to {MAX_VALUES} values")
        if not all(isinstance(v, str) and VALUE_PATTERN.fullmatch(v) for v in values):
            raise ValueError(f"every value of {axis} must be 1 to 100 characters of letters, digits and . _ : / @ + -, starting with a letter or digit")
        if len(set(values)) != len(values):
            raise ValueError(f"{axis} lists a value twice")
        out[axis] = sorted(values)
    return out or None


def encode(claim: dict[str, list[str]] | None) -> str | None:
    """The stored and forwarded form of a (normalised) claim: compact JSON with sorted keys, as smo_shared/scope.py encodes; `None` for no claim."""
    return None if not claim else json.dumps(claim, sort_keys=True, separators=(",", ":"))


def view(stored: str | None) -> dict[str, list[str]] | str | None:
    """A stored claim as the API shows it: the claim, `None` for none, or "INVALID" for a stored value that no longer reads as one (it still permits nothing)."""
    if not stored:
        return None
    try:
        return parse_claim(json.loads(stored))
    except ValueError:                                   # json.JSONDecodeError is a ValueError too
        return "INVALID"


def headers(username: str, stored: str | None) -> dict[str, str]:
    """The identity headers of a call made for `username`: the person, and the person's claim when it has one. A stored claim that cannot be read is forwarded
    as it is, which smo_shared/scope.py decodes as permitting nothing: a restriction is never dropped because it was damaged."""
    out = {ACTING_USER_HEADER: f"smo-gui:{username}"}
    if stored:
        out[ACTING_USER_SCOPE_HEADER] = stored
    return out


def shared_headers(stored: str | None) -> dict[str, str]:
    """The headers of a read the BFF shares between the users of one claim (summary counts, search): none for the unscoped (as before GUI-5), else the claim with
    `SHARED_PERSON` (a module believes a person's claim only beside a person)."""
    return {} if not stored else {ACTING_USER_HEADER: SHARED_PERSON, ACTING_USER_SCOPE_HEADER: stored}
