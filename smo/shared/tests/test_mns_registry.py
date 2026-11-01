"""The class registry (smo_shared/mns/registry.py, plan task S1.2): the classes of the vendored TS 28.541 / TS 28.623 YAML, their attribute schemas
(validation, unknown names, OpenAPI `nullable`), the containment they define, classes registered by hand, and DN handling (dn.py).
"""

import shutil

import pytest

from smo_shared.mns import ClassRegistry, MnsError, default_registry
from smo_shared.mns.dn import depth_of, dn_from_path, is_descendant, parent_of, parse_dn, path_of
from smo_shared.mns.registry import DEFAULT_SPEC_DIR, openapi_to_jsonschema


@pytest.fixture(scope="module")
def registry() -> ClassRegistry:
    return default_registry()


def test_every_class_of_the_vendored_yaml_resolves(registry):
    """Every `-Single` class of the TS 28.541 and TS 28.623 files is indexed (217 in the vendored release), and every `$ref` its attribute schemas
    reach resolves, so validation never stops at a missing file."""
    names = registry.names()
    assert len(names) == 217
    assert {"SubNetwork", "ManagedElement", "GNBDUFunction", "GNBCUCPFunction", "NRCellDU", "NRCellCU", "NRCellRelation", "PerfMetricJob",
            "AmfFunction", "NetworkSlice", "MnsRegistry"} <= set(names)
    assert all(registry.resolvable(name) for name in names)
    assert registry.missing_files == set()


def test_attribute_names(registry):
    """Attributes come from `attributes` members (NRCellDU, with ManagedFunction's), from `-Attr` schemas (SubNetwork) and from members written
    beside `attributes` (NROperatorCellDU)."""
    cell = registry.info("NRCellDU")
    assert cell.source == "TS28541_NrNrm.yaml"
    assert {"cellLocalId", "administrativeState", "nRTAC", "userLabel", "vnfParametersList"} <= cell.attribute_names
    assert registry.info("SubNetwork").attribute_names >= {"userLabel", "dnPrefix"}
    assert registry.info("NROperatorCellDU").attribute_names == {"administrativeState", "cellLocalId", "nRTAC", "plmnInfoList"}
    assert registry.info("NRNetwork").attribute_names == frozenset()


def test_containment_from_the_nrm(registry):
    """Children come from the `-Single` schema's members and `allOf` branches, through `-ncO` schemas in other files."""
    assert {"GNBDUFunction", "GNBCUCPFunction", "GNBCUUPFunction", "AlarmList", "MDAFunction"} <= registry.info("ManagedElement").children
    assert {"SubNetwork", "ManagedElement", "NRFrequency", "IntentHandlingFunction"} <= registry.info("SubNetwork").children
    assert {"NRCellDU", "NRSectorCarrier", "PerfMetricJob", "TraceJob"} <= registry.info("GNBDUFunction").children
    assert "NRCellRelation" in registry.info("NRCellCU").children
    assert registry.may_contain("GNBDUFunction", "NRCellDU")
    assert not registry.may_contain("SubNetwork", "NRCellDU")
    assert not registry.may_contain("NoSuchClass", "NRCellDU")
    assert registry.may_contain(None, "SubNetwork") and registry.may_contain(None, "ManagedElement")
    assert not registry.may_contain(None, "GNBDUFunction")


def test_validation(registry):
    """Valid attributes pass; a wrong type, a value outside an enum, a value outside a range and an unknown name are 422 naming the attribute."""
    registry.validate("NRCellDU", {"cellLocalId": 1, "administrativeState": "LOCKED", "nRTAC": "000001", "ssbFrequency": 10})
    registry.validate("SubNetwork", {})
    for attributes, bad in (({"cellLocalId": "1"}, ["cellLocalId"]), ({"administrativeState": "SHUT"}, ["administrativeState"]),
                            ({"ssbFrequency": -1}, ["ssbFrequency"]), ({"nope": 1, "zz": 2}, ["nope", "zz"]),
                            ({"cellLocalId": "1", "ssbFrequency": -1}, ["cellLocalId", "ssbFrequency"])):
        with pytest.raises(MnsError) as caught:
            registry.validate("NRCellDU", attributes)
        assert (caught.value.status, caught.value.type, caught.value.bad_attributes) == (422, "SCHEMA_VALIDATION_FAILED", bad)
    with pytest.raises(MnsError) as caught:
        registry.validate("NRCellDU", [])
    assert caught.value.bad_attributes == ["attributes"]


def test_unknown_class_is_422(registry):
    """A class with no schema is refused 422 CLASS_NOT_SUPPORTED, by `info`, by `validate`, and is not `resolvable`."""
    assert "NoSuchClass" not in registry and 5 not in registry
    for call in (lambda: registry.info("NoSuchClass"), lambda: registry.validate("NoSuchClass", {})):
        with pytest.raises(MnsError) as caught:
            call()
        assert (caught.value.status, caught.value.type, caught.value.bad_objects) == (422, "CLASS_NOT_SUPPORTED", ["NoSuchClass"])
    assert not registry.resolvable("NoSuchClass")


def test_nullable_is_converted():
    """`nullable: true` allows null next to a type, an enum, a `$ref` and a typeless schema; `nullable: false` is dropped."""
    assert openapi_to_jsonschema({"type": "string", "nullable": True}) == {"type": ["string", "null"]}
    assert openapi_to_jsonschema({"type": ["string"], "nullable": True}) == {"type": ["string", "null"]}
    assert openapi_to_jsonschema({"type": "string", "enum": ["a"], "nullable": True}) == {"type": ["string", "null"], "enum": ["a", None]}
    assert openapi_to_jsonschema({"type": "string", "enum": ["a", None], "nullable": True})["enum"] == ["a", None]
    assert openapi_to_jsonschema({"$ref": "#/x", "nullable": True}) == {"anyOf": [{"$ref": "#/x"}, {"type": "null"}]}
    assert openapi_to_jsonschema({"allOf": [{"type": "string"}], "nullable": True}) == {"anyOf": [{"allOf": [{"type": "string"}]}, {"type": "null"}]}
    assert openapi_to_jsonschema({"properties": {"a": {"type": "integer", "nullable": False}}}) == {"properties": {"a": {"type": "integer"}}}
    assert openapi_to_jsonschema([{"nullable": True, "type": "integer"}]) == [{"type": ["integer", "null"]}]


def test_registered_class():
    """A class registered by hand (an ADR 0011 [D] class) is validated against its own schema and contains what it is told to; it replaces a YAML
    class of the same name."""
    registry = ClassRegistry(class_files=())
    assert registry.names() == []
    registry.register("RappInstance", {"type": "object", "properties": {"state": {"type": "string", "enum": ["RUNNING"], "nullable": True},
                                                                        "name": {"type": "string"}}, "required": ["name"]}, children=["Probe"])
    registry.register("Probe", {"type": "object"})
    registry.validate("RappInstance", {"name": "x", "state": None})
    assert registry.info("RappInstance").attribute_names == {"state", "name"}
    with pytest.raises(MnsError) as caught:
        registry.validate("RappInstance", {"state": "RUNNING"})
    assert caught.value.bad_attributes == ["attributes"]
    assert registry.may_contain("RappInstance", "Probe") and registry.resolvable("RappInstance")
    assert registry.names() == ["Probe", "RappInstance"]


def test_register_replaces_a_yaml_class():
    """`register` wins over the YAML's schema for the same name, including a validator already built."""
    registry = ClassRegistry()
    registry.validate("NRCellDU", {"cellLocalId": 1})
    registry.register("NRCellDU", {"type": "object", "properties": {"only": {"type": "integer"}}})
    registry.validate("NRCellDU", {"only": 1})
    with pytest.raises(MnsError):
        registry.validate("NRCellDU", {"cellLocalId": 1})


def test_index_file_and_a_missing_reference(tmp_path):
    """A module adds a specification's file with `index_file`; a `$ref` into a file that is not there leaves the class unresolvable (and the file is
    recorded in `missing_files`), without failing the classes that do not need it."""
    shutil.copy(DEFAULT_SPEC_DIR / "TS28623_ComDefs.yaml", tmp_path)
    shutil.copy(DEFAULT_SPEC_DIR / "TS28623_GenericNrm.yaml", tmp_path)
    shutil.copy(DEFAULT_SPEC_DIR / "TS28104_MdaNrm.yaml", tmp_path)
    registry = ClassRegistry(tmp_path, class_files=("TS28623_GenericNrm.yaml",))
    assert "MDAFunction" not in registry
    assert registry.index_file("TS28104_MdaNrm.yaml") > 0
    assert registry.index_file("TS28104_MdaNrm.yaml") == 0
    assert "MDAFunction" in registry
    registry.info("SubNetwork")
    assert "TS28541_NrNrm.yaml" in registry.missing_files
    assert registry.resolvable("SubNetwork")
    assert not all(registry.resolvable(name) for name in registry.names())
    assert registry.index_file("TS99999_Nothing.yaml") == 0


def test_default_registry_is_shared():
    """One registry per spec directory per process."""
    assert default_registry() is default_registry()


# ---------------------------------------------------------------------------- DNs

def test_dn_forms():
    """A DN and its URI path map one to one; parent, depth and descendant follow the RDNs."""
    dn = "SubNetwork=A,ManagedElement=me-1.x,GNBDUFunction=1"
    assert dn_from_path("SubNetwork=A/ManagedElement=me-1.x/GNBDUFunction=1") == dn
    assert path_of(dn) == "SubNetwork=A/ManagedElement=me-1.x/GNBDUFunction=1"
    assert [str(rdn) for rdn in parse_dn(dn)] == ["SubNetwork=A", "ManagedElement=me-1.x", "GNBDUFunction=1"]
    assert parse_dn(dn)[1].moi_id == "me-1.x"
    assert parent_of(dn) == "SubNetwork=A,ManagedElement=me-1.x" and parent_of("SubNetwork=A") is None
    assert depth_of(dn) == 3 and depth_of("SubNetwork=A") == 1
    assert is_descendant(dn, "SubNetwork=A") and not is_descendant("SubNetwork=AB", "SubNetwork=A") and not is_descendant(dn, dn)


@pytest.mark.parametrize("text", ["", "SubNetwork", "SubNetwork=", "=A", "1Sub=A", "Sub Net=A", "SubNetwork=A B", "SubNetwork=A,", "SubNetwork=a%2C",
                                  "SubNetwork=A=B"])
def test_bad_dns(text):
    """An empty DN, a missing class or id, a class not starting with a letter, and characters a DN may not hold are a 400 INVALID_DN."""
    with pytest.raises(MnsError) as caught:
        parse_dn(text)
    assert (caught.value.status, caught.value.type) == (400, "INVALID_DN")
