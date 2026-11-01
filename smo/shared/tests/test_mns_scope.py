"""getMOIAttributes' selection (plan task S1.4): the four scope types with their levels, the filter grammar and its evaluation, attribute selection
(`attributes`, `fields`), the hierarchical and flat answers, and keyset paging through the `Link` header.

The HTTP tests run on in-memory SQLite and, when SMO_TEST_POSTGRES_URL is set, on real Postgres (mns_testmodule.py); the parser tests are pure.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from mns_testmodule import BASE, build_tree, make_app
from mns_testmodule import backend  # noqa: F401 (a pytest fixture)

from smo_shared.mns import MnsError
from smo_shared.mns.scope import MAX_DEPTH, MAX_FILTER_LENGTH, Scope, hierarchical, matches, parse_filter, parse_list, parse_scope, select
from smo_shared.mns.service import decode_cursor, encode_cursor

FLAT = {"accept": "application/vnd.3gpp.object-tree-flat+json"}
ME1 = "SubNetwork=A,ManagedElement=1"
DU1 = ME1 + ",GNBDUFunction=1"


def _flat(client, path, **params) -> list[str]:
    response = client.get(BASE + path, params=params, headers=FLAT)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.3gpp.object-tree-flat+json"
    return [item["objectInstance"] for item in response.json()]


# ---------------------------------------------------------------------------- scope

@pytest.mark.parametrize(("raw", "expected"), [
    (None, Scope("BASE_ONLY", 0)), ("", Scope("BASE_ONLY", 0)), ('{"scopeType": "BASE_ALL"}', Scope("BASE_ALL", 0)),
    ('{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 2}', Scope("BASE_NTH_LEVEL", 2)), ('{"scopeLevel": 3}', Scope("BASE_ONLY", 3)),
    ('{"scopeType": "BASE_SUBTREE", "scopeLevel": 0}', Scope("BASE_SUBTREE", 0)), ("{}", Scope("BASE_ONLY", 0)),
])
def test_parse_scope(raw, expected):
    """The scope parameter's JSON object, with BASE_ONLY when absent and level 0 where it does not matter."""
    assert parse_scope(raw) == expected


@pytest.mark.parametrize("raw", ["BASE_ALL", "[]", '{"scopeType": "ALL"}', '{"scopeType": "BASE_NTH_LEVEL"}', '{"scopeType": "BASE_SUBTREE"}',
                                 '{"scopeType": "BASE_SUBTREE", "scopeLevel": -1}', '{"scopeType": "BASE_SUBTREE", "scopeLevel": 1.5}',
                                 '{"scopeType": "BASE_SUBTREE", "scopeLevel": true}', '{"scopeType": "BASE_ALL", "other": 1}'])
def test_parse_scope_refuses(raw):
    """Not JSON, not an object, an unknown type or member, a missing or negative or non-integer level: a 400 naming `scope`."""
    with pytest.raises(MnsError) as caught:
        parse_scope(raw)
    assert (caught.value.status, caught.value.bad_query_params) == (400, ["scope"])


def test_depth_ranges():
    """Each scope type's depths, for a base at depth 3."""
    assert Scope("BASE_ONLY", 5).depth_range(3) == (3, 3)
    assert Scope("BASE_NTH_LEVEL", 2).depth_range(3) == (5, 5)
    assert Scope("BASE_SUBTREE", 2).depth_range(3) == (3, 5)
    assert Scope("BASE_ALL", 2).depth_range(3) == (3, None)


def test_each_scope_through_http(backend):
    """BASE_ONLY is the base; BASE_NTH_LEVEL is one level exactly (0 is the base); BASE_SUBTREE is the base down to the level; BASE_ALL is
    everything below; all in DN order, the sibling subtree of ManagedElement=2 never mixed in."""
    client, _ = make_app(backend)
    build_tree(client)
    cells = [DU1 + f",NRCellDU={n}" for n in (1, 2, 3)]
    assert _flat(client, "SubNetwork=A/ManagedElement=1") == [ME1]
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 0}') == [ME1]
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1}') == [DU1]
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 2}') == cells
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 3}') == []
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_SUBTREE", "scopeLevel": 1}') == [ME1, DU1]
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_SUBTREE", "scopeLevel": 9}') == [ME1, DU1, *cells]
    assert _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_ALL"}') == [ME1, DU1, *cells]
    assert len(_flat(client, "SubNetwork=A", scope='{"scopeType": "BASE_ALL"}')) == 9


def test_scope_does_not_take_in_a_sibling_with_a_longer_id(backend):
    """ManagedElement=1's subtree is not ManagedElement=10's or ManagedElement=1-b's, although their DNs start the same (the range query is exact)."""
    client, _ = make_app(backend)
    build_tree(client)
    for me in ("10", "1-b", "1_c"):
        client.put(BASE + f"SubNetwork=A/ManagedElement={me}", json={"id": me})
    assert ME1 + "0" not in _flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_ALL"}')
    assert len(_flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_ALL"}')) == 5


def test_get_of_a_missing_base_and_bad_parameters(backend):
    """A base that does not exist is a 404; a bad scope, filter, list or limit is a 400 ErrorResponseGet naming the parameter."""
    client, _ = make_app(backend)
    build_tree(client)
    missing = client.get(BASE + "SubNetwork=Nope")
    assert (missing.status_code, missing.json()["type"]) == (404, "MOI_NOT_FOUND")
    for params, name in (({"scope": "x"}, "scope"), ({"filter": "@.a =="}, "filter"), ({"attributes": "a,,b"}, "attributes"),
                         ({"fields": ","}, "fields"), ({"limit": "0"}, "limit"), ({"limit": "501"}, "limit"), ({"limit": "x"}, "limit"),
                         ({"cursor": "!!"}, "cursor")):
        response = client.get(BASE + "SubNetwork=A", params=params)
        assert (response.status_code, response.json()["badQueryParams"]) == (400, [name]), params
        assert "badAttributes" not in response.json()


# ---------------------------------------------------------------------------- the filter

RESOURCE = {"id": "1", "objectClass": "NRCellDU", "objectInstance": DU1 + ",NRCellDU=1",
            "attributes": {"administrativeState": "LOCKED", "cellLocalId": 5, "ratio": 0.5, "on": True, "none": None, "label": "it's",
                           "plmn": [{"mcc": "001"}, {"mcc": "002"}], "nested": {"a": {"b": 3}}}}


@pytest.mark.parametrize(("text", "expected"), [
    ("@.attributes.administrativeState == 'LOCKED'", True),
    ('@.attributes.administrativeState == "LOCKED"', True),
    ("@.attributes.administrativeState != 'LOCKED'", False),
    ("$[?(@.attributes.cellLocalId > 4)]", True),
    ("[?(@.attributes.cellLocalId >= 5 && @.attributes.cellLocalId <= 5)]", True),
    ("@.attributes.cellLocalId < 5", False),
    ("@.attributes.cellLocalId > 5", False),
    ("@.attributes.cellLocalId <= 4", False),
    ("@.attributes.cellLocalId >= 6", False),
    ("@.attributes.cellLocalId == 5.0", True),
    ("@.attributes.ratio < 1e0", True),
    ("@.attributes.on == true", True),
    ("@.attributes.on == 1", False),
    ("@.attributes.cellLocalId == true", False),
    ("@.attributes.none == null", True),
    ("@.attributes.none", False),
    ("@.attributes.cellLocalId", True),
    ("@.attributes.missing", False),
    ("@.attributes.missing != 'x'", False),
    ("@.attributes.missing == null", False),
    ("!@.attributes.missing", True),
    ("@.attributes.label == 'it\\'s'", True),
    ("@.attributes.plmn[1].mcc == '002'", True),
    ("@.attributes.plmn[-1].mcc == '002'", True),
    ("@.attributes.plmn[2].mcc == '002'", False),
    ("@['attributes']['nested'].a.b == 3", True),
    ("@.attributes.nested.a.b.c == 3", False),
    ("@.objectClass == 'NRCellDU' && (@.id == '2' || @.id == '1')", True),
    ("@.objectClass == 'NRCellDU' && @.id == '2' || @.id == '1'", True),
    ("!(@.id == '1')", False),
    ("@.attributes.administrativeState < 'M'", True),
    ("@.attributes.administrativeState > 5", False),
    ("@.attributes.cellLocalId == @.attributes.cellLocalId", True),
    ("@.attributes.plmn == @.attributes.plmn", True),
    ("5 == @.attributes.cellLocalId", True),
])
def test_filter_evaluation(text, expected):
    """The grammar's operators, literals and paths evaluated on one resource (comparisons with a missing value are false; a boolean is not a number)."""
    assert matches(parse_filter(text), RESOURCE) is expected


@pytest.mark.parametrize("text", ["@.a ==", "'x'", "5", "@.a = 1", "@.a == 1 &&", "(@.a == 1", "@.a == 1)", "$@.a", "@.[1]", "@[true]",
                                  "@.a == 'x", "@.a #", "@.a == \"\\q\"", "[?(@.a == 1", "@.a == 1 @.b"])
def test_filter_refuses(text):
    """Anything outside the grammar is a 400 naming `filter`."""
    with pytest.raises(MnsError) as caught:
        parse_filter(text)
    assert (caught.value.status, caught.value.bad_query_params) == (400, ["filter"])


def test_filter_limits():
    """No filter is no condition; a filter longer than MAX_FILTER_LENGTH, or nested deeper than MAX_DEPTH (by parentheses or `!`), is a 400."""
    assert parse_filter(None) is None and parse_filter("  ") is None and matches(None, {}) is True
    with pytest.raises(MnsError):
        parse_filter("@.id == '" + "x" * MAX_FILTER_LENGTH + "'")
    assert parse_filter("(" * (MAX_DEPTH - 1) + "@.id" + ")" * (MAX_DEPTH - 1)) is not None
    for deep in ("(" * MAX_DEPTH + "@.id" + ")" * MAX_DEPTH, "!" * (MAX_DEPTH + 1) + "@.id"):
        with pytest.raises(MnsError):
            parse_filter(deep)


def test_filter_through_http(backend):
    """The filter narrows the scoped set: the unlocked cells of the whole tree, and the base excluded when it does not match."""
    client, _ = make_app(backend)
    build_tree(client)
    unlocked = _flat(client, "SubNetwork=A", scope='{"scopeType": "BASE_ALL"}', filter="@.attributes.administrativeState == 'UNLOCKED'")
    assert unlocked == [DU1 + ",NRCellDU=2", DU1 + ",NRCellDU=3"]
    assert _flat(client, "SubNetwork=A", filter="@.objectClass == 'ManagedElement'") == []


# ---------------------------------------------------------------------------- attribute selection

def test_parse_list():
    """A comma-separated parameter, items trimmed; absent is None."""
    assert parse_list(None, "attributes") is None
    assert parse_list("a, b", "attributes") == ["a", "b"]


def test_select():
    """`attributes` keeps the named attributes; `fields` keeps paths (through arrays item by item); both: the union; identity always; neither: all."""
    assert select(RESOURCE, None, None) is RESOURCE
    assert select(RESOURCE, ["cellLocalId", "absent"], None) == {"id": "1", "objectClass": "NRCellDU", "objectInstance": RESOURCE["objectInstance"],
                                                                "attributes": {"cellLocalId": 5}}
    assert select(RESOURCE, None, ["/attributes/plmn/mcc", "attributes/nested/a/b"])["attributes"] == {"plmn": [{"mcc": "001"}, {"mcc": "002"}],
                                                                                                    "nested": {"a": {"b": 3}}}
    assert select(RESOURCE, ["on"], ["attributes/cellLocalId", "attributes/absent", "id", "attributes/nested/x"])["attributes"] == {
        "on": True, "cellLocalId": 5}
    assert select(RESOURCE, None, ["attributes"])["attributes"] == RESOURCE["attributes"]
    assert select(RESOURCE, [], None)["attributes"] == {}
    both = select(RESOURCE, ["nested"], ["attributes/nested/a/b"])
    assert both["attributes"] == {"nested": {"a": {"b": 3}}}


def test_select_merges_overlapping_array_fields():
    """Two fields through one array merge item by item."""
    resource = {"id": "1", "objectClass": "X", "objectInstance": "X=1", "attributes": {"list": [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]}}
    assert select(resource, None, ["attributes/list/a", "attributes/list/b"])["attributes"] == {"list": [{"a": 1, "b": 2}, {"a": 4, "b": 5}]}


def test_attribute_selection_through_http(backend):
    """`attributes` and `fields` on the wire."""
    client, _ = make_app(backend)
    build_tree(client)
    answer = client.get(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=2", params={"attributes": "administrativeState"}).json()
    assert answer["attributes"] == {"administrativeState": "UNLOCKED"}
    answer = client.get(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=2", params={"fields": "attributes/cellLocalId"}).json()
    assert answer["attributes"] == {"cellLocalId": 2}


# ---------------------------------------------------------------------------- the answer's form

def test_hierarchical():
    """Selected instances nest under their parents by class; an unselected one in between is a skeleton; an unselected base is a skeleton too."""
    cell = {"id": "1", "objectClass": "NRCellDU", "objectInstance": DU1 + ",NRCellDU=1", "attributes": {"x": 1}}
    du = {"id": "1", "objectClass": "GNBDUFunction", "objectInstance": DU1, "attributes": {"y": 2}}
    tree = hierarchical("SubNetwork=A", [cell])
    assert tree == {"id": "A", "objectClass": "SubNetwork", "objectInstance": "SubNetwork=A",
                    "ManagedElement": [{"id": "1", "objectClass": "ManagedElement", "objectInstance": ME1,
                                        "GNBDUFunction": [{"id": "1", "objectClass": "GNBDUFunction", "objectInstance": DU1, "NRCellDU": [cell]}]}]}
    full = hierarchical(ME1, [{"id": "1", "objectClass": "ManagedElement", "objectInstance": ME1, "attributes": {}}, du, cell])
    assert full["attributes"] == {} and full["GNBDUFunction"][0]["attributes"] == {"y": 2}
    assert full["GNBDUFunction"][0]["NRCellDU"] == [cell]
    late = hierarchical(ME1, [cell, du])
    assert late["GNBDUFunction"][0]["attributes"] == {"y": 2} and late["GNBDUFunction"][0]["NRCellDU"] == [cell]


def test_hierarchical_and_flat_through_http(backend):
    """The default and the hierarchical media type nest the scoped instances; the flat one lists them."""
    client, _ = make_app(backend)
    build_tree(client)
    response = client.get(BASE + "SubNetwork=A/ManagedElement=1", params={"scope": '{"scopeType": "BASE_ALL"}'},
                          headers={"accept": "application/vnd.3gpp.object-tree-hierarchical+json"})
    assert response.headers["content-type"] == "application/vnd.3gpp.object-tree-hierarchical+json"
    tree = response.json()
    assert [cell["id"] for cell in tree["GNBDUFunction"][0]["NRCellDU"]] == ["1", "2", "3"]
    assert len(_flat(client, "SubNetwork=A/ManagedElement=1", scope='{"scopeType": "BASE_ALL"}')) == 5


# ---------------------------------------------------------------------------- paging

def test_cursor_round_trip():
    """A cursor is the base64url of a DN, without padding; anything else is refused."""
    assert decode_cursor(encode_cursor(DU1)) == DU1
    assert "=" not in encode_cursor("SubNetwork=AB")
    for bad in ("!!", encode_cursor("not a dn"), "_w"):
        with pytest.raises(MnsError):
            decode_cursor(bad)


def test_paging_through_http(backend):
    """`limit` pages in DN order; the `Link` header's next URI carries the cursor and the other parameters; the last page has no Link; the pages
    together are the unpaged answer."""
    client, _ = make_app(backend)
    build_tree(client)
    params = {"scope": '{"scopeType": "BASE_ALL"}', "filter": "@.objectClass != 'SubNetwork'"}
    everything = _flat(client, "SubNetwork=A", **params)
    assert len(everything) == 8
    pages, query = [], {**params, "limit": "3"}
    for _ in range(10):                                   # a bounded walk: a next link that never ends fails here, not by hanging
        response = client.get(BASE + "SubNetwork=A", params=query, headers=FLAT)
        pages.append([item["objectInstance"] for item in response.json()])
        link = response.headers.get("link")
        if link is None:
            break
        url = link[1:link.index(">")]
        assert link.endswith('; rel="next"') and urlsplit(url).path == "/ProvMnS/19.2.0/SubNetwork=A"
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        assert query["filter"] == params["filter"] and query["limit"] == "3"
    assert [len(page) for page in pages] == [3, 3, 2]
    assert [dn for page in pages for dn in page] == everything


def test_paging_is_stable_while_the_tree_changes(backend):
    """An instance added before the cursor, or one removed from a page already read, does not shift what the next page holds (keyset paging)."""
    client, _ = make_app(backend)
    build_tree(client)
    first = client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "3"}, headers=FLAT)
    link = first.headers["link"]
    client.put(BASE + "SubNetwork=A/ManagedElement=0", json={"id": "0"})
    client.delete(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU=1")
    second = client.get(link[1:link.index(">")], headers=FLAT)
    assert [item["objectInstance"] for item in second.json()][0] == DU1 + ",NRCellDU=2"


def test_exactly_full_last_page_has_no_link(backend):
    """When the matches end exactly at a page boundary there is no next page to announce."""
    client, _ = make_app(backend)
    build_tree(client)
    response = client.get(BASE + "SubNetwork=A/ManagedElement=1", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "5"}, headers=FLAT)
    assert len(response.json()) == 5 and "link" not in response.headers


def test_paging_behind_a_selective_filter(backend):
    """With a filter that drops most rows, a page still holds `limit` matches and its link leads to the rest; the result is the same as unpaged."""
    client, _ = make_app(backend)
    build_tree(client)
    params = {"scope": '{"scopeType": "BASE_ALL"}', "filter": "@.attributes.cellLocalId >= 3"}
    response = client.get(BASE + "SubNetwork=A", params={**params, "limit": "1"}, headers=FLAT)
    assert [item["id"] for item in response.json()] == ["3"]
    link = response.headers["link"]
    rest = client.get(link[1:link.index(">")], headers=FLAT)
    assert [item["id"] for item in rest.json()] == ["4"] and "link" not in rest.headers
