"""SEC-15.5: `build_edit_config_rpc` (both the `<managed-object>` shape of `netconf_client` and the YANG shape of `yang_payload`) escapes what it writes into the XML and refuses names it cannot.

Each test builds an RPC from hostile input, parses it back with `defusedxml` and checks the structure: the values come back as the same text, no element or attribute was added, and an
unacceptable name, operation or datastore raises ValueError before anything is built. No network. Run with
`cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/test_netconf_xml_escaping.py -q`.
"""

import defusedxml.ElementTree as ET
import httpx
import pytest

from app import netconf_client, yang_payload
from app.netconf_client import build_edit_config_rpc as build_plain

PROFILE = yang_payload.PROFILES[next(iter(yang_payload.PROFILES))]


def build_yang(message_id, target_ref, changes, operation="merge", function_ref=None, target="running"):
    """The YANG-shaped RPC, with the same argument order as the plain builder so one table of inputs drives both."""
    return yang_payload.build_edit_config_rpc(PROFILE, message_id, target_ref, changes, operation, function_ref, target)


BUILDERS = [pytest.param(build_plain, id="managed-object"), pytest.param(build_yang, id="yang")]


def _local(node):
    """The tag of an element without its namespace."""
    return node.tag.rsplit("}", 1)[-1]


def _leaves(rpc):
    """`{local name: text}` of the attribute elements of an edit-config RPC, whichever shape it has: the children of the one managed-object (or list entry) element."""
    root = ET.fromstring(rpc)
    config = next(iter(root.iter(f"{{{netconf_client.NETCONF_BASE_NS}}}config")))
    entry = config[0] if _local(config[0]) == "managed-object" else config[0][0]
    return {_local(child): child.text for child in entry if _local(child) not in (PROFILE.key_leaf,)}


# Table: values that would break out of the element or change the document if written raw: markup, an ampersand, quotes, CDATA-like text, an entity reference, an injected element.
HOSTILE_VALUES = [
    "<script>alert(1)</script>", "a & b", "1 < 2 > 0", 'say "hi" and \'bye\'', "]]>", "<![CDATA[ x ]]>", "]]><injected/>", "&lt;already&gt;", "&#x3c;",
    "</adminState><admin-state>UNLOCKED</admin-state><x>", "<!-- c -->", "<?pi x?>", "<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]>&e;", "line1\nline2\ttab",
]


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_a_hostile_value_comes_back_as_the_same_text_and_adds_no_element(build, value):
    """The value is text in the RPC: parsing it back gives the same string, and the only child element is the one attribute."""
    rpc = build("m-1", "ME-1", {"adminState": value})
    leaves = _leaves(rpc)
    assert len(leaves) == 1 and list(leaves.values())[0] == value
    assert "<injected" not in rpc and "<script" not in rpc and "<![CDATA[" not in rpc and "<!DOCTYPE" not in rpc


@pytest.mark.parametrize("build", BUILDERS)
def test_non_string_values_are_written_as_their_text(build):
    """Numbers and booleans are accepted as before and written as their `str()`; the escape does not change them."""
    leaves = _leaves(build("m-1", "ME-1", {"txPower": 23, "enabled": True}))
    assert list(leaves.values()) == ["23", "True"]


# Table: names that would add an element, an attribute, a namespace prefix or markup if written as given.
BAD_NAMES = ["a><b", "a b", 'x y="1"', "a/b", "1abc", "", "-x", "a:b", "xmlns", "XMLfoo", "a\n", "<a>", "a" * 129, "é"]


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("name", BAD_NAMES)
def test_an_attribute_name_that_is_not_an_xml_name_is_refused(build, name):
    """A name that could change the structure raises ValueError and no RPC is built; the same rule holds for both shapes."""
    with pytest.raises(ValueError):
        build("m-1", "ME-1", {name: "v"})


@pytest.mark.parametrize("build", BUILDERS)
def test_an_ordinary_name_is_accepted(build):
    """camelCase, dotted and hyphenated names (what real attributes look like) still build."""
    for name in ("administrativeState", "tx-power", "a.b_c1", "_x"):
        assert build("m-1", "ME-1", {name: "v"})


@pytest.mark.parametrize("build", BUILDERS)
def test_a_value_with_a_character_xml_cannot_carry_is_refused(build):
    """A NUL or another C0 control character cannot be in an XML 1.0 document at all, so the value is refused rather than sent."""
    for bad in ("a\x00b", "a\x1fb", "a￾b"):
        with pytest.raises(ValueError):
            build("m-1", "ME-1", {"adminState": bad})


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("operation", ['merge" injected="1', "merge><x/", "MERGE", "", "merge ", None])
def test_an_unknown_operation_is_refused(build, operation):
    """The operation goes into an attribute, so only the five values of RFC 6241 section 7.2 are accepted (the managed-object shape did not check it)."""
    with pytest.raises(ValueError):
        build("m-1", "ME-1", {"a": "v"}, operation)


@pytest.mark.parametrize("build", BUILDERS)
def test_an_unknown_datastore_is_refused(build):
    """The datastore is an element name (`<running/>`), so a value outside running and candidate raises instead of being written."""
    with pytest.raises(ValueError):
        build("m-1", "ME-1", {"a": "v"}, "merge", None, "running/><x")


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("ref", ['ME-1" injected="1', "ME-1'><x/>", "a&b<c", "ME-1\"><evil/>"])
def test_a_reference_or_message_id_cannot_end_its_attribute(build, ref):
    """A quote or markup in the element reference or the message id stays inside its attribute or text (or the reference is refused as a bad DN): the document parses and has no extra element or attribute."""
    for args in (("m-1", ref), (ref, "ME-1")):
        try:
            rpc = build(*args, {"a": "v"})
        except ValueError:  # the YANG shape also refuses a reference with `=` that is not a well-formed DN: a refusal is as safe as an escape
            continue
        root = ET.fromstring(rpc)
        names = {_local(node) for node in root.iter()}
        assert "evil" not in names and "x" not in names
        assert "injected" not in root.attrib and not any("injected" in node.attrib for node in root.iter())


def test_the_plain_message_id_attribute_is_quoted_and_a_delete_carries_no_leaves():
    """The plain shape writes `message-id` through quoteattr (the same placement as before for an ordinary id) and a delete with no changes is still built."""
    rpc = build_plain("msg-1", "ME-1", {}, operation="delete")
    assert 'message-id="msg-1"' in rpc and 'operation="delete"' in rpc
    assert 'message-id="msg-1"' in netconf_client.build_get_config_rpc("msg-1", "ME-1")


def test_the_http_client_reports_a_refused_edit_as_not_applied_without_sending(monkeypatch):
    """`send_edit_config` over HTTP returns a non-applied EditResult (NETCONF_RPC_FAILED) for an unacceptable name or value and never posts anything, as the SSH path already did for an unknown operation."""
    posts = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append(a))
    result = netconf_client.send_edit_config("http://adaptor", "ME-1", {"a><b": "v"}, "m-1")
    assert not result and result.reason == "NETCONF_RPC_FAILED" and posts == []
