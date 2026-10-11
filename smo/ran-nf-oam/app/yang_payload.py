"""Payloads for a NETCONF server that has a real YANG model (PR-SB-1.5).

The build's own adaptor (and its mock) understand `<managed-object ref=...>`; a real server understands only its models' data nodes.
An ssh adaptor URI may name one of the models below with `?model=<name>`: `ssh://netconf@host:830?model=smo-lab`. Without it the
`<managed-object>` shape is sent, as before. A profile says where a managed object lives in the model: the container, the list, the key
leaf, and which RDN of the managed function (or element) reference is the key. Attribute names are camelCase on the SMO side and
kebab-case in YANG (`administrativeState` is `administrative-state`).

Only the models in `PROFILES` are known; a model that is not is refused when the URI is parsed, so a typo never falls back to a shape
the server will not understand.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape, quoteattr

from .ldn import leaf_id
from .netconf_client import NETCONF_BASE_NS, xml_name, xml_text


@dataclass(frozen=True)
class Profile:
    """Where a managed object lives in a real YANG model: the container and list names, the key leaf and its namespace; `rdn_class` and `containment` say how a list entry is named as a DN when the model is walked.
    """
    namespace: str
    container: str
    list_name: str
    key_leaf: str
    # PR-SB-6.2: where the list's entries sit in the containment tree, below the element root: the classes of the entries and the RDNs above them
    rdn_class: str = ""
    containment: tuple[str, ...] = ()


# smo-lab is not a 3GPP model: its `cell` entries are mapped onto the 3GPP names the runbook already uses (`GNBDUFunction=1,NRCellDU=<id>`), so
# a walk of the lab yields the DNs the write routes take.
PROFILES = {"smo-lab": Profile("urn:smo:lab", "lab", "cell", "id", rdn_class="NRCellDU", containment=("GNBDUFunction=1",))}


def model_of(adaptor_uri: str) -> str | None:
    """The `model` named in the URI's query, or None. ValueError for an unknown model or more than one."""
    query = parse_qs(urlsplit(adaptor_uri).query, keep_blank_values=True)
    unknown = set(query) - {"model", "datastore"}
    if unknown:
        raise ValueError(f"unknown adaptor URI option {sorted(unknown)[0]!r} (only model= and datastore=)")
    values = query.get("model", [])
    if not values:
        return None
    if len(values) > 1 or values[0] not in PROFILES:
        raise ValueError(f"unknown model {values[-1]!r}; known: {', '.join(sorted(PROFILES))}")
    return values[0]


def datastore_of(adaptor_uri: str) -> str:
    """`running` (the default) or `candidate`, from the URI's `datastore` option. ValueError for anything else or a repeat."""
    values = parse_qs(urlsplit(adaptor_uri).query, keep_blank_values=True).get("datastore", [])
    if not values:
        return "running"
    if len(values) > 1 or values[0] not in ("running", "candidate"):
        raise ValueError(f"unknown datastore {values[-1]!r}; use running or candidate")
    return values[0]


def to_yang_name(attribute: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "-" + m.group(1).lower(), attribute)


def to_attribute_name(leaf: str) -> str:
    """The SMO camelCase attribute name for a YANG kebab-case leaf (`administrative-state` gives `administrativeState`); the inverse of `to_yang_name`.
    """
    head, *rest = leaf.split("-")
    return head + "".join(part.capitalize() for part in rest)


def _key(target_ref: str, managed_function_ref: str | None) -> str:
    return leaf_id(managed_function_ref or target_ref) or (managed_function_ref or target_ref)


def build_get_config_rpc(profile: Profile, message_id: str, target_ref: str, managed_function_ref: str | None = None) -> str:
    """The `<get-config>` RPC (subtree filter on the running datastore) that reads the one list entry the reference names; `message_id` and the key are XML-escaped.
    """
    key = escape(_key(target_ref, managed_function_ref))
    return (f'<rpc message-id={quoteattr(message_id)} xmlns="{NETCONF_BASE_NS}"><get-config><source><running/></source>'
            f'<filter type="subtree"><{profile.container} xmlns="{profile.namespace}"><{profile.list_name}>'
            f"<{profile.key_leaf}>{key}</{profile.key_leaf}></{profile.list_name}></{profile.container}></filter></get-config></rpc>")


def config_attributes(profile: Profile, root, target_ref: str, managed_function_ref: str | None = None) -> dict | None:
    """The leaves of the list entry the reference names, as SMO attribute names; None when the reply has no `<data>`, {} when
    the entry does not exist."""
    local = lambda node: node.tag.rsplit("}", 1)[-1]  # noqa: E731
    data = next((c for c in root if local(c) == "data"), None)
    if data is None:
        return None
    key = _key(target_ref, managed_function_ref)
    for entry in data.iter():
        if local(entry) != profile.list_name:
            continue
        leaves = {local(child): (child.text or "").strip() for child in entry}
        if leaves.get(profile.key_leaf) == key:
            return {to_attribute_name(name): value for name, value in leaves.items() if name != profile.key_leaf}
    return {}


OPERATIONS = ("merge", "replace", "create", "delete", "remove")


def build_edit_config_rpc(profile: Profile, message_id: str, target_ref: str, attribute_changes: dict, operation: str = "merge",
                          managed_function_ref: str | None = None, target: str = "running") -> str:
    """An `<edit-config>` on the model's list entry (RFC 6241 section 7.2): the `operation` attribute is on the entry, the key leaf
    names it, and each attribute is a leaf. A delete or remove carries the key only. `target` is `running` or `candidate`. Names and values are checked and escaped
    as in `netconf_client.build_edit_config_rpc`; ValueError when one is not acceptable."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown edit operation {operation!r}")
    if target not in ("running", "candidate"):
        raise ValueError(f"unknown datastore {target!r}")
    # SEC-15.5: a leaf name must be an XML name and a value must be text XML can carry (`netconf_client.xml_name`, `xml_text`); the message id is written with `quoteattr`, so a quote in it
    # cannot end the attribute. ValueError, with nothing built, for any that is not acceptable.
    leaves = "" if operation in ("delete", "remove") else "".join(
        f"<{xml_name(to_yang_name(name))}>{xml_text(value)}</{to_yang_name(name)}>" for name, value in attribute_changes.items())
    key = escape(_key(target_ref, managed_function_ref))
    return (f'<rpc message-id={quoteattr(message_id)} xmlns="{NETCONF_BASE_NS}"><edit-config><target><{target}/></target><config>'
            f'<{profile.container} xmlns="{profile.namespace}"><{profile.list_name} xmlns:nc="{NETCONF_BASE_NS}" nc:operation="{operation}">'
            f"<{profile.key_leaf}>{key}</{profile.key_leaf}>{leaves}</{profile.list_name}></{profile.container}></config></edit-config></rpc>")


def build_walk_rpc(profile: Profile, message_id: str) -> str:
    """A subtree `<get-config>` of the model's whole container: every list entry (PR-SB-6.2)."""
    return (f'<rpc message-id={quoteattr(message_id)} xmlns="{NETCONF_BASE_NS}"><get-config><source><running/></source>'
            f'<filter type="subtree"><{profile.container} xmlns="{profile.namespace}"/></filter></get-config></rpc>')


def walk_paths(profile: Profile, root, ) -> list[str] | None:
    """The DNs below the element root that a whole-container reply reports, as paths relative to the root (`GNBDUFunction=1,NRCellDU=101`), the
    containment RDNs first; None when the reply has no `<data>`. Entries without a key are skipped."""
    local = lambda node: node.tag.rsplit("}", 1)[-1]  # noqa: E731
    data = next((c for c in root if local(c) == "data"), None)
    if data is None:
        return None
    paths = []
    for entry in data.iter():
        if local(entry) != profile.list_name:
            continue
        key = next(((child.text or "").strip() for child in entry if local(child) == profile.key_leaf), "")
        if key:
            paths.append(",".join((*profile.containment, f"{profile.rdn_class}={key}")))
    return sorted(set(paths))
