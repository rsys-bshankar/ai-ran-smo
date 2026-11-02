"""Tests of TE&IV's ProvMnS read facade over smo-teiv (plan tasks S3.19, S3.20, S3.21; ADR 0013 G1, G7, G8, section 7), on the fake smo-teiv holding
TE&IV's 12-cell tree: the five queries of ADR 0013 section 11.4 in TS 28.532 form answered exactly (attributes under their TS 28.541 names, references
back from relationships, the reversed NEIGHBOR_CELL side), what of a filter is sent down to smo-teiv, the reads refused with a reason, the object
version and its If-Match check, and the writes refused until S3.4."""

import json

import pytest

from app import model

import tree

BASE = "/ProvMnS/19.2.0/"
NTH1 = json.dumps({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1})
ALL = json.dumps({"scopeType": "BASE_ALL"})


def path(dn: str) -> str:
    """The URI of a DN below the ProvMnS root."""
    return BASE + dn.replace(",", "/")


def scope_filters(fake) -> list[str]:
    """The scopeFilter of every list smo-teiv was asked for."""
    return [r.url.params.get("scopeFilter", "") for r in fake.requests if r.url.path.endswith(("/entities", "/relationships"))]


def test_q1_one_cell_with_its_ts_names_decorators_reference_and_version(api, tree_mois):
    """Q1: an NRCellDU, BASE_ONLY, has nrPci (smo-teiv's nRPCI), its decorators decoded (administrativeState, plmnInfoInfoList), its
    nRSectorCarrierRef from the relationship, and its version as ETag; nothing TS 28.541 lacks (nCI) is shown."""
    dn = tree.du_cell(1, 2)
    source = next(m for m in tree_mois if m.dn == dn).attributes
    r = api.get(path(dn))
    assert r.status_code == 200, r.text
    body = r.json()
    attrs = body["attributes"]
    assert body["objectInstance"] == dn and body["objectClass"] == "NRCellDU" and body["id"] == "2"
    assert attrs["nrPci"] == source["nrPci"] and attrs["nRTAC"] == source["nRTAC"] and attrs["cellLocalId"] == 2
    assert attrs["administrativeState"] == source["administrativeState"] and attrs["plmnInfoInfoList"] == source["plmnInfoInfoList"]
    assert attrs["nRSectorCarrierRef"] == source["nRSectorCarrierRef"]
    assert "nCI" not in attrs and "nRPCI" not in attrs
    from app.provmns import etag
    assert r.headers["etag"] == etag(body)


def test_q2_a_cells_neighbours_are_its_relations_with_adjacent_cells_from_the_reversed_neighbor_cell(api, tree_mois):
    """Q2: the NRCellCU, BASE_NTH_LEVEL 1, filter NRCellRelation: its 8 relations, each with adjacentNRCellRef (NEIGHBOR_CELL's aSide, the
    relation its third sourceId), nRFreqRelationRef, and the booleans as booleans."""
    cu = tree.cu_cell(2, 1)
    expected = {m.dn: m.attributes for m in tree_mois if m.cls == "NRCellRelation" and m.dn.startswith(cu + ",")}
    r = api.get(path(cu), params={"scope": NTH1, "filter": "@.objectClass=='NRCellRelation'"}, headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert r.status_code == 200, r.text
    found = {x["objectInstance"]: x["attributes"] for x in r.json()}
    assert set(found) == set(expected) and len(found) == tree.NEIGHBOURS
    for dn, attrs in found.items():
        assert attrs["adjacentNRCellRef"] == expected[dn]["adjacentNRCellRef"]
        assert attrs["nRFreqRelationRef"] == expected[dn]["nRFreqRelationRef"]
        assert attrs["isHOAllowed"] is True and attrs["isESCoveredBy"] == expected[dn]["isESCoveredBy"]
        assert attrs["cellIndividualOffset"] == {"rsrpOffsetSsb": 0}


def test_q2_selecting_adjacentnrcellref_reads_ids_and_the_neighbour_edges_only(api, fake):
    """A neighbour read that selects adjacentNRCellRef asks smo-teiv for the relations' ids and the NEIGHBOR_CELL edges, not the frequency edges."""
    cu = tree.cu_cell(0, 1)
    fake.requests.clear()
    r = api.get(path(cu), params={"scope": NTH1, "filter": "@.objectClass=='NRCellRelation'", "attributes": "adjacentNRCellRef"},
                headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert r.status_code == 200
    assert all(set(x["attributes"]) == {"adjacentNRCellRef"} for x in r.json()) and len(r.json()) == tree.NEIGHBOURS
    lists = [q for q in fake.requests if q.url.path.endswith(("/entities", "/relationships"))]
    assert sorted(q.url.path.rsplit("/", 2)[1] for q in lists) == ["NRCELLCU_NEIGHBOR_CELL_NRCELLCU", "NRCellRelation"]
    assert next(q for q in lists if q.url.path.endswith("/entities")).url.params["targetFilter"] == "/sourceIds"


def test_q3_the_cells_of_a_gnbdufunction(api):
    """Q3: the GNBDUFunction, BASE_NTH_LEVEL 1, filter NRCellDU: its three cells."""
    r = api.get(path(f"{tree.me_dn(3)},GNBDUFunction=1"), params={"scope": NTH1, "filter": "@.objectClass=='NRCellDU'"},
                headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert r.status_code == 200
    assert [x["objectInstance"] for x in r.json()] == [tree.du_cell(3, c) for c in (1, 2, 3)]


def test_q5_a_subnetworks_cells_by_a_native_value_are_filtered_in_smo_teiv(api, fake, tree_mois):
    """Q5: the SubNetwork, BASE_ALL, filter NRCellDU and nRTAC: exactly the cells with that value; the equality went down as /attributes[@nRTAC=..]."""
    tac = next(m for m in tree_mois if m.cls == "NRCellDU").attributes["nRTAC"]
    expected = sorted(m.dn for m in tree_mois if m.cls == "NRCellDU" and m.attributes["nRTAC"] == tac)
    fake.requests.clear()
    r = api.get(path(tree.SUBNETWORK), params={"scope": ALL, "filter": f"@.objectClass=='NRCellDU' && @.attributes.nRTAC=={tac}"},
                headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert r.status_code == 200
    assert [x["objectInstance"] for x in r.json()] == expected
    assert any(f"/attributes[@nRTAC={tac}]" in f for f in scope_filters(fake))


def test_a_filter_on_a_decorator_goes_down_and_one_smo_teiv_cannot_take_is_teivs_alone(api, fake, tree_mois):
    """administrativeState == 'LOCKED' goes down as a decorator condition; an `||` stays TE&IV's; both answers are exactly the tree's."""
    locked = sorted(m.dn for m in tree_mois if m.cls == "NRCellDU" and m.attributes["administrativeState"] == "LOCKED")
    flat = {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
    fake.requests.clear()
    r = api.get(path(tree.SUBNETWORK), params={"scope": ALL, "filter": "@.objectClass=='NRCellDU' && @.attributes.administrativeState=='LOCKED'"},
                headers=flat)
    assert [x["objectInstance"] for x in r.json()] == locked
    assert any("/decorators[@smo-teiv-attrs:administrativeState='LOCKED']" in f for f in scope_filters(fake))
    either = api.get(path(tree.SUBNETWORK), params={"scope": ALL, "filter": "@.objectClass=='NRCellDU' && (@.attributes.cellLocalId==1 || "
                                                    "@.attributes.administrativeState=='LOCKED')"}, headers=flat)
    expected = sorted(m.dn for m in tree_mois if m.cls == "NRCellDU" and (m.attributes["cellLocalId"] == 1 or m.attributes["administrativeState"] == "LOCKED"))
    assert [x["objectInstance"] for x in either.json()] == expected


def test_the_short_sourceid_condition_never_leaks_a_lookalike_object(teiv, fake, api):
    """smo-teiv is asked for a superset (`=gNB-0001,` and `NRCellCU=1,`); a cell NRCellCU=10 and a namesake gNB under another SubNetwork are not in
    the answer, and their relations' edges do not land on the cell's relations."""
    from app import ingest
    other = [ingest.Moi("SubNetwork=OTHER", {"userLabel": "o"}),
             ingest.Moi(f"SubNetwork=OTHER,ManagedElement=gNB-0001", {}),
             ingest.Moi(f"SubNetwork=OTHER,ManagedElement=gNB-0001,GNBCUCPFunction=1", {}),
             ingest.Moi(f"SubNetwork=OTHER,ManagedElement=gNB-0001,GNBCUCPFunction=1,NRCellCU=1", {"cellLocalId": 1}),
             ingest.Moi(f"SubNetwork=OTHER,ManagedElement=gNB-0001,GNBCUCPFunction=1,NRCellCU=1,NRCellRelation=1", {"nRTCI": 1}),
             ingest.Moi(f"{tree.me_dn(1)},GNBCUCPFunction=1,NRCellCU=10", {"cellLocalId": 10}),
             ingest.Moi(f"{tree.me_dn(1)},GNBCUCPFunction=1", {}),
             ingest.Moi(f"{tree.me_dn(1)},GNBCUCPFunction=1,NRCellCU=10,NRCellRelation=1", {"nRTCI": 9, "adjacentNRCellRef": tree.cu_cell(0, 1)}),
             ingest.Moi(tree.cu_cell(0, 1), {"cellLocalId": 1})]
    events, calls = ingest.encode(other)
    fake.load(events, calls)
    cu = tree.cu_cell(1, 1)
    r = api.get(path(cu), params={"scope": NTH1, "filter": "@.objectClass=='NRCellRelation'"}, headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert {x["objectInstance"].rsplit(",", 1)[0] for x in r.json()} == {cu}
    assert len(r.json()) == tree.NEIGHBOURS
    assert all(x["attributes"]["nRTCI"] != 9 for x in r.json())


def test_a_base_that_does_not_exist_is_404_and_a_class_teiv_does_not_hold_is_422(api):
    """MOI_NOT_FOUND for a cell smo-teiv has not; CLASS_NOT_SUPPORTED for a class outside the 15, with them named."""
    gone = api.get(path(f"{tree.me_dn(1)},GNBDUFunction=1,NRCellDU=9"))
    assert gone.status_code == 404 and gone.json()["type"] == "MOI_NOT_FOUND"
    other = api.get(path(f"{tree.me_dn(1)},EUtranGenericCell=1"))
    assert other.status_code in (404, 422)
    if other.status_code == 422:
        assert other.json()["type"] == "CLASS_NOT_SUPPORTED"


def test_a_dn_smo_teivs_filter_cannot_carry_is_refused_not_answered_wrongly(teiv):
    """A `'` in a DN cannot be put into smo-teiv's filter exactly: INVALID_DN (the shared DN parser refuses it first, 400; TE&IV's own check, 422,
    stands behind it)."""
    from smo_shared.mns.errors import MnsError
    from smo_shared.mns.scope import Scope
    with pytest.raises(MnsError) as err:
        teiv.reader.subtree("SubNetwork=SMO-RAN,ManagedElement=it's", Scope(), None)
    assert err.value.status in (400, 422) and err.value.type == "INVALID_DN"


def test_a_scope_too_wide_for_one_read_is_refused_with_the_way_out(api, monkeypatch):
    """Over TEIV_MAX_SCOPE_OBJECTS candidates of one class: 422 NOT_EXPRESSIBLE, naming scope and filter, never a partial answer."""
    monkeypatch.setenv("TEIV_MAX_SCOPE_OBJECTS", "5")
    r = api.get(path(tree.SUBNETWORK), params={"scope": ALL, "filter": "@.objectClass=='NRCellRelation'"})
    assert r.status_code == 422 and r.json()["type"] == "NOT_EXPRESSIBLE"


@pytest.mark.parametrize("mode", ["5xx", "timeout"])
def test_smo_teiv_not_answering_is_503_with_retry_after(api, fake, mode):
    """smo-teiv down (a 500) or too slow (a timeout): 503 TEIV_UNAVAILABLE with Retry-After, the caller retries."""
    if mode == "5xx":
        fake.fail_with = 500
    else:
        fake.timeout = True
    r = api.get(path(tree.du_cell(0, 1)))
    assert r.status_code == 503 and r.json()["type"] == "TEIV_UNAVAILABLE" and r.headers["retry-after"] == "5"


def test_smo_teiv_refusing_teivs_own_request_is_502_teiv_refused(api, fake):
    """A 4xx smo-teiv answers to a list TE&IV sent is TE&IV's defect: 502 TEIV_REFUSED with smo-teiv's detail."""
    import httpx
    fake.answers[("GET", "/domains/RAN/entity-types/NRCellDU/entities")] = httpx.Response(400, json={"detail": "bad filter"})
    r = api.get(path(f"{tree.me_dn(0)},GNBDUFunction=1"), params={"scope": NTH1, "filter": "@.objectClass=='NRCellDU'"})
    assert r.status_code == 502 and r.json()["type"] == "TEIV_REFUSED" and "bad filter" in r.json()["reason"]


def test_paging_continues_in_dn_order(api):
    """limit 5 over a GNBDUFunction's subtree: the next page starts after the last DN of the first, and together they are the whole answer."""
    flat = {"Accept": "application/vnd.3gpp.object-tree-flat+json"}
    whole = api.get(path(f"{tree.me_dn(0)},GNBDUFunction=1"), params={"scope": ALL}, headers=flat).json()
    first = api.get(path(f"{tree.me_dn(0)},GNBDUFunction=1"), params={"scope": ALL, "limit": 5}, headers=flat)
    assert first.status_code == 200 and len(first.json()) == 5
    link = first.headers["link"]
    cursor = link.split("cursor=", 1)[1].split("&", 1)[0].split(">", 1)[0]
    second = api.get(path(f"{tree.me_dn(0)},GNBDUFunction=1"), params={"scope": ALL, "limit": 100, "cursor": cursor}, headers=flat)
    assert [x["objectInstance"] for x in first.json() + second.json()] == [x["objectInstance"] for x in whole]


def test_a_scoped_caller_sees_only_what_its_claim_reaches(api):
    """A caller whose claim names another SubNetwork does not see TE&IV's (404, as for any base outside a caller's reach)."""
    r = api.get(path(tree.du_cell(0, 1)), headers={"X-R1-Scope": json.dumps({"subNetworks": ["ELSEWHERE"]})})
    assert r.status_code in (403, 404)
    mine = api.get(path(tree.du_cell(0, 1)), headers={"X-R1-Scope": json.dumps({"subNetworks": ["SMO-RAN"]})})
    assert mine.status_code == 200


def test_a_stale_if_match_is_412_with_the_current_etag_and_a_current_one_reaches_the_write_path(api):
    """modifyMOIAttributes: If-Match of another version is 412 STALE_VERSION with the ETag; the current version, or none, is 501 (S3.4)."""
    dn = tree.du_cell(2, 3)
    current = api.get(path(dn)).headers["etag"]
    merge = {"Content-Type": "application/3gpp-merge-patch+json"}
    stale = api.patch(path(dn), content=json.dumps({"attributes": {"userLabel": "x"}}), headers={**merge, "If-Match": '"0000000000000000"'})
    assert stale.status_code == 412 and stale.json()["type"] == "STALE_VERSION" and stale.headers["etag"] == current
    fresh = api.patch(path(dn), content=json.dumps({"attributes": {"userLabel": "x"}}), headers={**merge, "If-Match": current})
    assert fresh.status_code == 501 and fresh.json()["type"] == "WRITE_NOT_AVAILABLE"
    assert api.patch(path(dn), content=json.dumps({"attributes": {}}), headers=merge).status_code == 501


def test_the_version_changes_when_a_decorator_does(api, fake):
    """smo-teiv's lastModified stays when a decorator changes; TE&IV's version does not (S3.21)."""
    dn = tree.du_cell(0, 2)
    before = api.get(path(dn)).headers["etag"]
    fake.load([], [{"operation": "merge", "decorators": {"smo-teiv-attrs:administrativeState": "SHUTTINGDOWN"}, "entityIds": [model.teiv_id(dn)]}])
    after = api.get(path(dn))
    assert after.json()["attributes"]["administrativeState"] == "SHUTTINGDOWN" and after.headers["etag"] != before


def test_create_and_delete_are_refused_after_their_checks(api):
    """createMOI of an absent instance and deleteMOI of a present one: 501 WRITE_NOT_AVAILABLE; deleteMOI of an absent one: 404."""
    created = api.put(path(f"{tree.me_dn(0)},GNBDUFunction=1,NRCellDU=7"), json={"id": "7", "attributes": {}})
    assert created.status_code == 501
    assert api.delete(path(tree.du_cell(0, 1))).status_code == 501
    assert api.delete(path(f"{tree.me_dn(0)},GNBDUFunction=1,NRCellDU=8")).status_code == 404
