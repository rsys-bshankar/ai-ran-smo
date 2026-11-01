"""Tests of TE&IV's model map, decorator schema and encoder (plan tasks S3.19 to S3.21; ADR 0013 G1, section 7): the 15 classes and their DNs, the
booleans held as the extension's enumeration, the decorator values that survive smo-teiv's unescaped SQL, the generated schema being current, and
what the encoder writes for a tree (the reversed NEIGHBOR_CELL side, the batch limit)."""

import json

import pytest

from app import attrs, ingest, model

from fake_teiv import FakeTeiv


def test_the_fifteen_classes_and_their_dns_map_one_to_one():
    """Every class has a domain and an entity type; a DN's smo-teiv id is the DN behind `urn:3gpp:dn:` and back; class_of reads the last RDN."""
    assert len(model.CLASSES) == 15
    dn = "SubNetwork=SMO-RAN,ManagedElement=gNB-0001,GNBDUFunction=1,NRCellDU=2"
    assert model.teiv_id(dn) == "urn:3gpp:dn:" + dn and model.dn_of(model.teiv_id(dn)) == dn
    assert model.class_of(dn) == "NRCellDU"
    with pytest.raises(ValueError):
        model.dn_of("urn:other:" + dn)


def test_a_boolean_is_held_as_the_extensions_enumeration_and_read_back_as_a_boolean():
    """isHOAllowed true is stored `true` (Boolean_Type) and read back True; an attribute TS 28.541 lacks is not shown."""
    rel = model.CLASSES["NRCellRelation"]
    assert model.to_teiv(rel, "isHOAllowed", True) == "true" and model.to_teiv(rel, "isHOAllowed", False) == "false"
    assert model.from_teiv(rel, "isHOAllowed", "true") == ("isHOAllowed", True)
    assert model.from_teiv(model.CLASSES["NRCellDU"], "nCI", 5) is None
    assert model.from_teiv(model.CLASSES["NRCellDU"], "nRPCI", 7) == ("nrPci", 7)


def test_containment_reaches_a_relation_from_its_subnetwork():
    """The classes below a SubNetwork include the relation four levels down and the CCOFunction one level down."""
    below = model.descendants("SubNetwork")
    assert "NRCellRelation" in below and "CCOFunction" in below and "NRCellDU" in below


@pytest.mark.parametrize("value", ["UNLOCKED", 7, True, None, 1.5, [{"plmnId": {"mcc": "001"}}], {"a": "x'y\"z"}, "it's", "j:looks-encoded"])
def test_a_decorator_value_round_trips_and_is_safe_in_smo_teivs_sql(value):
    """What attrs.encode writes decodes to the value; text that is not plain (a quote, a structure) is the `j:` form, whose characters are safe."""
    text = attrs.encode(value)
    assert attrs.decode(text) == value
    assert attrs.SAFE.fullmatch(text) or (text.startswith("j:") and attrs.SAFE.fullmatch(text[2:]))


def test_the_committed_decorator_schema_is_current_and_valid_for_smo_teiv():
    """app/smo-teiv-attrs.yang is what the class registry generates (`python -m app.attrs --write`), every leaf a string, no enum without a value."""
    from smo_shared.mns.registry import default_registry
    text = attrs.generate(default_registry())
    assert attrs.YANG.read_text() == text, "stale: run python -m app.attrs --write from smo/teiv"
    assert len(attrs.names(text)) >= 90 and "administrativeState" in attrs.names(text)
    assert "enum" not in text                      # smo-teiv refuses an enum without a value; the decorators are strings


def test_ensure_schema_uploads_once_as_application_yang():
    """The first call uploads the schema (smo-teiv takes only application/yang); the next finds it present."""
    from app.client import SmoTeivClient
    fake = FakeTeiv()
    client = SmoTeivClient("http://teiv-exposure:8080", transport=fake.transport())
    assert attrs.ensure_schema(client) == "created"
    assert attrs.ensure_schema(client) == "present"
    assert attrs.MODULE in fake.schemas


def test_the_encoder_writes_the_neighbour_reversed_with_the_relation_as_third_source(tree_mois):
    """adjacentNRCellRef becomes ADJACENT and NEIGHBOR_CELL with the neighbour as aSide, the cell as bSide and the relation as sourceIds[2]."""
    events, _ = ingest.encode(tree_mois)
    neighbours = [r for e in events for block in e["relationships"] for kind, rels in block.items() if kind == model.NEIGHBOR_CELL for r in rels]
    relation = next(m for m in tree_mois if m.cls == "NRCellRelation")
    edge = next(r for r in neighbours if r["sourceIds"][2] == model.teiv_id(relation.dn))
    assert edge["aSide"] == model.teiv_id(relation.attributes["adjacentNRCellRef"])
    assert edge["bSide"] == model.teiv_id(relation.dn.rsplit(",", 1)[0])


def test_decorator_calls_stay_within_smo_teivs_batch_limit():
    """A tree of 1,000 cells needs updateDecorator calls of at most 100 entities each, every one a merge of TE&IV's own decorators."""
    import tree
    _, calls = ingest.encode(tree.synthetic(400))
    assert calls and all(len(c["entityIds"]) <= ingest.DECORATOR_BATCH and c["operation"] == "merge" for c in calls)
    assert all(k.startswith(attrs.MODULE + ":") for c in calls for k in c["decorators"])
    json.dumps(calls)


def test_the_encoder_refuses_a_class_teiv_does_not_hold():
    """A Moi of a class outside model.CLASSES is a ValueError, never written."""
    with pytest.raises(ValueError):
        ingest.encode([ingest.Moi("SubNetwork=S,ManagedElement=1,EUtranCellFDD=1")])
