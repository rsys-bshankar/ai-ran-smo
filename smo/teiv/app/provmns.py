"""TE&IV's ProvMnS producer (plan tasks S3.19, S3.21): the shared router of `smo_shared.mns` mounted read-only on smo-teiv (ADR 0013 section 7).

`TeivProvMnS` is `smo_shared.mns.ProvMnS` with its store replaced by smo-teiv for the four operations, so everything the router and the shared service
do around an operation stays theirs (the URI and the version, the error bodies, the hierarchical and flat answers, the `Link` paging header, the S1.5
checks: caller and scope, MSAC, the trace, `smo_provmns_http_duration_seconds`):

  getMOIAttributes      the scope and the filter answered from smo-teiv by `reader.TeivReader` (G1, G8), then, as the shared service does: the caller's
                        scope and MSAC (`checks.authorize` on the base, `checks.visible` on every instance below it), the attribute selection, and
                        keyset paging in DN order with the same cursor
  modifyMOIAttributes,  the caller is checked first (403 for a target outside its scope, as for any write), then the instance (404), then `If-Match`
  createMOI, deleteMOI  against the instance's version (`reader.version`, S3.21): a stale version is 412 STALE_VERSION with the current ETag. A write
                        that passes all of that is 501 WRITE_NOT_AVAILABLE: TE&IV's write path (ProvMnS writes turned into smo-teiv ingestion events,
                        the only writer, D6) is plan task S3.4, and the notifications on it S3.22.

A getMOIAttributes of one instance (BASE_ONLY) answers its version as the `ETag` header (`EtagMiddleware` in main.py), so a writer can read, then
modify with `If-Match`, once S3.4 is there.
"""

from smo_shared.mns import ProvMnS
from smo_shared.mns.checks import INTERNAL, Caller
from smo_shared.mns.errors import MnsError
from smo_shared.mns.scope import Scope, select as select_fields
from smo_shared.mns.service import DEFAULT_LIMIT, MAX_LIMIT, Page, decode_cursor, encode_cursor

from .reader import TeivReader, version

WRITES_LATER = "TE&IV's ProvMnS writes are plan task S3.4 (ADR 0013 G6): until then TE&IV reads only"


def etag(resource: dict) -> str:
    """The ETag of a resource: its version, quoted."""
    return f'"{version(resource)}"'


class TeivProvMnS(ProvMnS):
    """The ProvMnS producer of TE&IV (module description); `reader` is the smo-teiv reader."""

    def __init__(self, *, reader: TeivReader, **kwargs):
        super().__init__(**kwargs)
        self.reader = reader

    def get_moi_attributes(self, db, dn: str, scope: Scope | None = None, filter_tree: tuple | None = None, attributes: list[str] | None = None,
                           fields: list[str] | None = None, limit: int = DEFAULT_LIMIT, cursor: str | None = None, caller: Caller = INTERNAL) -> Page:
        """getMOIAttributes from smo-teiv: the same contract as the shared service's (limit 1..MAX_LIMIT, 404 for a base outside the caller's reach,
        instances below it filtered by the caller's reach, DN order, the cursor of the next page)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise MnsError(400, "INVALID_QUERY", f"limit is an integer from 1 to {MAX_LIMIT}", bad_query_params=["limit"])
        self.checks.authorize(caller, "getMOIAttributes", dn)
        self._require_served(dn.rsplit(",", 1)[-1].split("=", 1)[0])
        after = decode_cursor(cursor) if cursor else None
        # the attributes selected (fields: everything, as their paths may name any), so the reader skips the references nobody asked for
        found = self.reader.subtree(dn, scope or Scope(), filter_tree, set(attributes) if attributes is not None and fields is None else None)
        unrestricted = caller.scope is None and self.checks.msac.provider(caller) is None
        selected = [r for r in found if (after is None or r["objectInstance"] > after)
                    and (unrestricted or r["objectInstance"] == dn or self.checks.visible(caller, r["objectInstance"]))]
        page = selected[:limit]
        next_cursor = encode_cursor(page[-1]["objectInstance"]) if len(selected) > limit else None
        return Page([select_fields(item, attributes, fields) for item in page], next_cursor)

    def _write(self, operation: str, dn: str, if_match: str | None, caller: Caller) -> None:
        """The checks every write passes before TE&IV's write path (S3.4) would run: the caller's reach, the class, the instance, `If-Match`."""
        self.checks.authorize(caller, operation, dn)
        self._require_served(dn.rsplit(",", 1)[-1].split("=", 1)[0])
        current = self.reader.base(dn)
        if current is None:
            if operation == "createMOI" and if_match is None:
                raise MnsError(501, "WRITE_NOT_AVAILABLE", WRITES_LATER, bad_objects=[dn])
            raise MnsError(404 if if_match is None else 412, "MOI_NOT_FOUND" if if_match is None else "STALE_VERSION",
                           f"{dn} does not exist", bad_objects=[dn])
        check_if_match(if_match, current, dn)
        raise MnsError(501, "WRITE_NOT_AVAILABLE", WRITES_LATER, bad_objects=[dn])

    def create_moi(self, db, dn: str, body, if_match: str | None = None, caller: Caller = INTERNAL):
        """createMOI: refused after the checks (module description)."""
        self._write("createMOI", dn, if_match, caller)

    def modify_moi_attributes(self, db, dn: str, media_type: str, body, if_match: str | None = None, caller: Caller = INTERNAL):
        """modifyMOIAttributes: refused after the checks, a stale If-Match first (module description)."""
        self._write("modifyMOIAttributes", dn, if_match, caller)

    def delete_moi(self, db, dn: str, if_match: str | None = None, caller: Caller = INTERNAL):
        """deleteMOI: refused after the checks (module description)."""
        self._write("deleteMOI", dn, if_match, caller)


def check_if_match(if_match: str | None, current: dict, dn: str) -> None:
    """RFC 9110 If-Match against the instance's version (S3.21): absent passes, `*` passes for an instance that exists, a list of ETags must name the
    current one (weak tags compare equal); otherwise 412 STALE_VERSION with the current ETag."""
    if if_match is None or if_match.strip() == "*":
        return
    tags = [tag.strip().removeprefix("W/") for tag in if_match.split(",")]
    if etag(current) not in tags:
        raise MnsError(412, "STALE_VERSION", f"{dn} is at version {version(current)}, not the one If-Match names", bad_objects=[dn],
                       headers={"ETag": etag(current)})
