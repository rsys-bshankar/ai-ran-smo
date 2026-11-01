"""Tests for teiv/model/check_extension.py (plan task S3.24): TE&IV's extension modules pass the TE&IV data model's extension rules, and a module that
breaks each rule (an augment, the data model's namespace or domain, an entity type or a relationship without the mandatory groupings, a side that is not
an instance-identifier, a role taken twice on one type) is reported."""

import importlib.util
import textwrap
from pathlib import Path

import pytest

try:
    import pyang  # noqa: F401  (only to know whether it is there: requirements/contracts.txt, which the Contracts job installs)
    HAVE_PYANG = True
except ImportError:
    HAVE_PYANG = False

MODEL = Path(__file__).resolve().parents[1] / "model"
_loaded = {}


def checker():
    """teiv/model/check_extension.py, loaded on first use; skips the calling test, with the reason, where pyang is not installed. The tests are collected
    the same everywhere (docs/test-counts/teiv.md): only their run needs pyang."""
    if not HAVE_PYANG:
        pytest.skip("pyang is in requirements/contracts.txt: the Contracts job runs these tests")
    if "ce" not in _loaded:
        spec = importlib.util.spec_from_file_location("check_extension", MODEL / "check_extension.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded["ce"] = module
    return _loaded["ce"]


GOOD = """
module smo-teiv-ext-test {
    yang-version 1.1;
    namespace "urn:smo:teiv-ext-test";
    prefix ext-test;
    import o-ran-smo-teiv-common-yang-types { prefix or-teiv-types; }
    import o-ran-smo-teiv-common-yang-extensions { prefix or-teiv-yext; }
    import o-ran-smo-teiv-ran { prefix or-teiv-ran; }
    description "test";
    revision "2026-11-01" { description "test"; or-teiv-yext:label 0.1.0; }
    or-teiv-yext:domain EXT_TEST;
    list Thing {
        key id;
        description "test";
        uses or-teiv-types:Top_Grp_Type;
        uses or-teiv-types:Origin_Entity_Mapping_Grp;
    }
    or-teiv-yext:biDirectionalTopologyRelationship THING_SERVES_NRCELLDU {
        uses or-teiv-types:Top_Grp_Type;
        uses or-teiv-types:Origin_Relationship_Mapping_Grp;
        leaf serving-thing { description "a"; type instance-identifier; or-teiv-yext:aSide Thing; }
        leaf-list served-nrCellDu { description "b"; type instance-identifier; or-teiv-yext:bSide or-teiv-ran:NRCellDU; }
    }
}
"""


def write(tmp_path: Path, text: str, name: str = "smo-teiv-ext-test") -> Path:
    """Writes `text` as the YANG file `name`.yang under `tmp_path`."""
    f = tmp_path / f"{name}.yang"
    f.write_text(textwrap.dedent(text))
    return f


def findings(tmp_path: Path, text: str) -> list[str]:
    """The checker's findings on one module."""
    ce = checker()
    return ce.check(ce.parse(write(tmp_path, text)))


def test_teivs_own_extension_modules_pass():
    """The two extension modules of teiv/model/ break none of the rules E1 to E7."""
    assert checker().main([]) == 0


def test_a_module_that_follows_the_rules_has_no_finding(tmp_path):
    """The fixture module: its own domain, both groupings on the type and the relationship, two instance-identifier sides."""
    assert findings(tmp_path, GOOD) == []


@pytest.mark.parametrize("change, rule", [
    (("    or-teiv-yext:domain EXT_TEST;", "    or-teiv-yext:domain EXT_TEST;\n    augment /or-teiv-types:decorators { leaf x { type string; } }"), "E1"),
    (("    or-teiv-yext:domain EXT_TEST;", "    or-teiv-yext:domain EXT_TEST;\n    deviation /or-teiv-ran:NRCellDU { deviate not-supported; }"), "E1"),
    (('namespace "urn:smo:teiv-ext-test";', 'namespace "urn:o-ran:smo-teiv-test";'), "E2"),
    (("or-teiv-yext:domain EXT_TEST;", "or-teiv-yext:domain RAN;"), "E3"),
    (("or-teiv-yext:domain EXT_TEST;", ""), "E3"),
    (("        uses or-teiv-types:Origin_Entity_Mapping_Grp;\n", ""), "E4"),
    (("        uses or-teiv-types:Origin_Relationship_Mapping_Grp;\n", ""), "E5"),
    (('leaf serving-thing { description "a"; type instance-identifier;', 'leaf serving-thing { description "a"; type string;'), "E5"),
    (("or-teiv-yext:aSide Thing;", ""), "E5"),
    (("or-teiv-yext:label 0.1.0;", "or-teiv-yext:label v1;"), "E6"),
])
def test_each_broken_rule_is_reported(tmp_path, change, rule):
    """One change to the fixture breaks one rule, and the finding names it."""
    old, new = change
    assert old in GOOD
    assert any(f" {rule} " in f for f in findings(tmp_path, GOOD.replace(old, new)))


def test_a_role_already_on_the_type_is_reported(tmp_path):
    """E7: a second relationship that gives NRCellDU the role a base module already gave it."""
    ce = checker()
    base = write(tmp_path, GOOD.replace("smo-teiv-ext-test", "o-ran-smo-teiv-test").replace("urn:smo:", "urn:o-ran:"), "o-ran-smo-teiv-test")
    ext = write(tmp_path, GOOD.replace("THING_SERVES_NRCELLDU", "OTHER_SERVES_NRCELLDU"))
    found = ce.check_roles([ce.parse(ext)], [ce.parse(base)])
    assert found and all(" E7 " in f for f in found)
    assert ce.check_roles([ce.parse(ext)], []) == []
