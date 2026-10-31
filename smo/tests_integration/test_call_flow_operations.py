"""Every message of call flows 34-37 is an operation the schema bundle has (plan task S0.6, done when "every operation named in call flows 34-37 has a
schema"; the flows are S0.9).

A message is a mermaid sequence arrow between two different participants (`A->>B: text`); a dashed answer (`-->>`) needs nothing. Each message must
carry at least one of:
- an operation written `<operation> [<class>] (<spec> <clause>)`, where the operation is a TS 28.532 ProvMnS operation or notification on a class the
  receiver's contract lists with that operation (the sender's, for a notification; any contract, when the participant has none: an rApp, the operator),
  a bundle path's `operationId` or the API title or `operationId` of a published file a bundle path `$ref`s (CAPIF), or a NETCONF operation of RFC 6241
  on the published YANG modules the bundle names (`yang/modules.txt`);
- an ADR 0011 section 6 id `[D-nn]`;
- `[pending: <interface>]`, where some `x-smo-pending` entry of the bundle names that interface.
A [D] class or path needs its own [D] id in the message. Every `[D-nn]` anywhere in the four flows is listed in ADR 0011 section 6, no message uses a
route appendix A marks "delete", and every participant is one of ADR 0008's fourteen services, the operator or OSS, an rApp instance, an O-DU/O-CU (O1)
or an O-Cloud (O2).
Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_call_flow_operations.py -q
"""

import re
from pathlib import Path

import pytest
import yaml

SMO = Path(__file__).resolve().parent.parent
FLOWS = sorted((SMO / "docs" / "call-flows").glob("3[4-7]-*.md"))
BUNDLE = SMO.parent / "specs" / "smo-schema-bundle"
MODULES = BUNDLE / "openapi" / "modules"
ADR_0011 = SMO / "docs" / "adr" / "0011-standard-interfaces-only.md"
APPENDIX_A = SMO / "docs" / "adr" / "0011-appendix-a-route-map.md"

PROVMNS = {"createMOI", "getMOIAttributes", "modifyMOIAttributes", "deleteMOI"}
NOTIFICATIONS = {"notifyMOICreation", "notifyMOIDeletion", "notifyMOIAttributeValueChanges", "notifyMOIChanges"}
NETCONF = {"get-config", "edit-config", "validate", "commit", "discard-changes", "lock", "unlock", "get"}  # RFC 6241 base operations
PARTICIPANTS = {"SME": "sme", "R1 Termination": None, "TE&IV": "teiv", "RAN NF OAM": "ran-nf-oam", "DME": "dme", "MDAF": "mdaf", "AIMgF": "aimgf",
                "Onboarding": None, "rApp Management": "rapp-mgmt", "NFO": "nfo", "FOCOM": None, "SO SMOS": "so-smos", "SA SMOS": "sa-smos",
                "GUI BFF": None, "Operator/OSS": None, "rApp instance": None, "O-DU/O-CU": None, "O-Cloud": None}
_ARROW = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(-->>|->>|-x|--x|-\)|--\))\s*([A-Za-z0-9_]+)\s*:\s*(.*)$")
_PARTICIPANT = re.compile(r"^\s*(?:participant|actor)\s+([A-Za-z0-9_]+)(?:\s+as\s+(.+))?$")
_OPERATION = re.compile(r"([A-Za-z_][\w-]*)(?: ([A-Z][A-Za-z0-9]*))? \((?:TS|RFC|R1GAP|O-RAN) [^)]*\)")
_DESIGN = re.compile(r"\[(D-\d+)\]")
_PENDING = re.compile(r"\[pending: ([^\]]+)\]")


def _load(path: Path):
    """A YAML document."""
    return yaml.safe_load(path.read_text())


def _contracts() -> dict[str, dict]:
    """{module: contract} of every module contract of the bundle."""
    return {p.stem: _load(p) for p in sorted(MODULES.glob("*.yaml"))}


def _classes(contract: dict) -> dict[str, tuple[set[str], str | None]]:
    """{class: (operations, [D] id or None)} of a contract's `x-smo-classes`."""
    return {c["class"]: (set(c["operations"]), c.get("x-smo-design")) for c in contract.get("x-smo-classes", [])}


def _path_operations(contracts: dict[str, dict]) -> dict[str, str | None]:
    """{operation name: [D] id or None}: every bundle path's operationId, and for a path that `$ref`s a published file, that file's API title and the
    operationIds of the referenced path item."""
    out: dict[str, str | None] = {}
    for doc in contracts.values():
        for item in doc.get("paths", {}).values():
            if "$ref" in item:
                file_part, _, pointer = item["$ref"].partition("#")
                target = (MODULES / file_part).resolve()
                if "5G_APIs" not in str(target):
                    continue
                spec = _load(target)
                out[spec["info"]["title"]] = None
                node = spec
                for part in [p for p in pointer.split("/") if p]:
                    node = node[part.replace("~1", "/").replace("~0", "~")]
                out.update({op["operationId"]: None for op in node.values() if isinstance(op, dict) and "operationId" in op})
                continue
            for op in item.values():
                if isinstance(op, dict) and op.get("operationId"):
                    out[op["operationId"]] = op.get("x-smo-design")
    return out


def _design_ids() -> set[str]:
    """The ids of ADR 0011 section 6."""
    section = ADR_0011.read_text().split("## 6.", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| (D-\d+) \|", section, re.M))


def _deleted_routes() -> list[re.Pattern]:
    """A pattern per route appendix A marks "delete" (path parameters match any segment)."""
    out = []
    for row in APPENDIX_A.read_text().splitlines():
        cells = [c.strip() for c in row.split("|")]
        if len(cells) > 6 and cells[5] == "delete":
            path = cells[4].strip("`")
            out.append(re.compile(re.sub(r"\\\{[^}]*\\\}", r"[^/\\s]+", re.escape(path)) + r"(?![\w/-])"))
    return out


def _pending_texts(contracts: dict[str, dict]) -> list[str]:
    """Every `x-smo-pending` entry of the bundle."""
    return [entry for doc in contracts.values() for entry in doc.get("x-smo-pending") or []]


def _diagrams(path: Path) -> list[str]:
    """The mermaid blocks of a document."""
    return re.findall(r"```mermaid\n(.*?)```", path.read_text(), re.S)


def _participant_module(label: str) -> tuple[bool, str | None]:
    """(allowed, contract module) for a participant label: one of PARTICIPANTS, optionally followed by a qualifier."""
    for name, module in PARTICIPANTS.items():
        if label == name or label.startswith(name + " "):
            return True, module
    return False, None


def flow_problems(path: Path, contracts: dict[str, dict]) -> list[str]:
    """Every rule of the module docstring that a call-flow document breaks, one line each."""
    problems = []
    classes = {m: _classes(doc) for m, doc in contracts.items()}
    every_class: dict[str, tuple[set[str], str | None]] = {}
    for per_module in classes.values():
        for name, (ops, design) in per_module.items():
            known = every_class.setdefault(name, (set(), design))
            every_class[name] = (known[0] | ops, known[1] or design)
    paths = _path_operations(contracts)
    pending = _pending_texts(contracts)
    deleted = _deleted_routes()
    yang = (BUNDLE / "yang" / "modules.txt").read_text()
    for n, block in enumerate(_diagrams(path), 1):
        where = f"{path.name} diagram {n}"
        modules: dict[str, str | None] = {}
        for line in block.splitlines():
            if m := _PARTICIPANT.match(line):
                label = (m.group(2) or m.group(1)).strip()
                allowed, modules[m.group(1)] = _participant_module(label)
                if not allowed:
                    problems.append(f"{where}: participant {label!r} is not one of ADR 0008's services or the four outside parties")
                continue
            m = _ARROW.match(line)
            if not m:
                continue
            sender, arrow, receiver, text = m.groups()
            for pattern in deleted:
                if pattern.search(text):
                    problems.append(f"{where}: {text!r} uses a route appendix A marks delete")
            if sender == receiver or arrow.startswith("--"):
                continue
            designs = _DESIGN.findall(text)
            pendings = _PENDING.findall(text)
            for interface in pendings:
                if not any(interface in entry for entry in pending):
                    problems.append(f"{where}: [pending: {interface}] is named by no x-smo-pending entry of the bundle")
            operations = [] if pendings else _OPERATION.findall(text)
            for op, cls in operations:
                if op in PROVMNS or op in NOTIFICATIONS:
                    owner = modules.get(sender if op in NOTIFICATIONS else receiver)
                    table = classes.get(owner) if owner else None
                    entry = (table or every_class).get(cls) if cls else None
                    if not cls:
                        if op in PROVMNS:
                            problems.append(f"{where}: {op} names no class in {text!r}")
                    elif entry is None:
                        problems.append(f"{where}: class {cls} of {op} is not in the {owner or 'bundle'} contract")
                    elif op in PROVMNS and op not in entry[0]:
                        problems.append(f"{where}: the {owner or 'bundle'} contract does not offer {op} on {cls}")
                    elif entry[1] and entry[1] not in designs:
                        problems.append(f"{where}: [D] class {cls} needs [{entry[1]}] in {text!r}")
                elif op in NETCONF:
                    if "RFC 6241" not in text or not yang.strip():
                        problems.append(f"{where}: NETCONF {op} needs RFC 6241 and the bundle's yang/modules.txt")
                elif op in paths:
                    if paths[op] and paths[op] not in designs:
                        problems.append(f"{where}: {op} is a [D] path and needs [{paths[op]}] in {text!r}")
                    if cls and cls not in paths:
                        problems.append(f"{where}: {cls} is not an operation of {op}")
                else:
                    problems.append(f"{where}: {op} is not an operation of the schema bundle ({text!r})")
            if not (operations or designs or pendings):
                problems.append(f"{where}: {sender}->>{receiver} {text!r} is neither a bundle operation, a [D-nn] step nor a named pending interface")
    ids = _design_ids()
    for design in sorted(set(_DESIGN.findall(path.read_text())) - ids):
        problems.append(f"{path.name}: [{design}] is not listed in ADR 0011 section 6")
    return problems


def test_flows_34_to_37_exist():
    """The four flows of plan task S0.9 are there, and number 33 stays unused."""
    assert [p.name[:2] for p in FLOWS] == ["34", "35", "36", "37"]
    assert not list((SMO / "docs" / "call-flows").glob("33-*.md"))


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.stem)
def test_every_message_is_a_bundle_operation(path):
    """Each message of the flow is a standard operation the bundle has, a listed [D] step or a pending interface the bundle names."""
    assert flow_problems(path, _contracts()) == []


def test_the_check_refuses_an_unknown_operation_a_missing_d_id_and_an_unlisted_one(tmp_path):
    """A made-up operation, a [D] class without its id, an unlisted [D-99], a pending interface the bundle does not name, a plain message and an
    unknown participant are each reported."""
    doc = tmp_path / "34-x.md"
    doc.write_text("""```mermaid
sequenceDiagram
    participant SO as SO SMOS
    participant RAM as rApp Management
    participant X as Somebody
    SO->>RAM: launchRapp RappInstance (TS 28.532 §11.1.1.1)
    SO->>RAM: createMOI RappInstance (TS 28.532 §11.1.1.1)
    SO->>RAM: [D-99] something
    SO->>RAM: [pending: Warp drive] engage
    SO->>RAM: just a call
    SO->>RAM: deleteMOI RappRegistration (TS 28.532 §11.1.1.4) [D-19]
```""")
    problems = "\n".join(flow_problems(doc, _contracts()))
    assert "launchRapp is not an operation" in problems
    assert "needs [D-19]" in problems
    assert "[D-99] is not listed" in problems
    assert "Warp drive" in problems
    assert "neither a bundle operation" in problems
    assert "'Somebody' is not one of" in problems
    assert "does not offer deleteMOI on RappRegistration" not in problems


def test_a_route_appendix_a_deletes_is_refused(tmp_path):
    """A message naming a route appendix A marks delete (here autonomy dispatch's sweep) is reported."""
    route = next(p for p in _deleted_routes())
    doc = tmp_path / "35-x.md"
    sample = re.sub(r"\[\^/\\s\]\+", "abc", route.pattern.split("(?!")[0]).replace("\\", "")
    doc.write_text(f"```mermaid\nsequenceDiagram\n    participant A as SO SMOS\n    participant B as DME\n    A->>B: POST {sample} [D-04]\n```\n")
    assert any("appendix A marks delete" in p for p in flow_problems(doc, _contracts()))
