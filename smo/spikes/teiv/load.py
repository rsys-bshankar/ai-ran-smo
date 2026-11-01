#!/usr/bin/env python3
"""The TE&IV spike's loader (plan task S0.13, ADR 0013): turn a TS 28.541 managed-object tree into smo-teiv 0.3.0's ingestion format and say what
does not fit. Spike code: no module imports it, and it imports no module except RAN NF OAM's NETCONF client for the `--netconf` source.

Sources of the tree (a list of `Moi`: DN, class, attributes):
  --ran-nf-oam URL --element REF ...   what RAN NF OAM holds for those elements (GET /managed-objects/{root}/subtree): source a) of S0.13
  --oran-sim-log FILE ...             the simulators' own logs (scripts/oran_sim_check.py reads them): each one's element and NETCONF URI for the two
                                       sources above
  --netconf ssh://user@host:830 ...    the simulator's own ManagedElement read with RAN NF OAM's NETCONF client (app.netconf_ssh, credential `oransim`):
                                       what a) becomes once RAN NF OAM walks a 3GPP model (S3.3); used because RAN NF OAM's tree of an oran-sim element is
                                       today its root alone (its walk needs a YANG profile that has no 3GPP NRM yet)
  --synthetic N                        source b): N NRCellDU in a SubNetwork, three per gNB, each gNB a ManagedElement with a GNBDUFunction (cells,
                                       sector carriers, a CommonBeamformingFunction per carrier) and a GNBCUCPFunction (NRCellCU, about eight
                                       NRCellRelation per cell, TS 28.541 contains them under the NRCellCU)

Output: CloudEvents of type `ran-logical-topology.merge` (smo-teiv's ingestion contract: entities and relationships by YANG module and type, ids
`urn:3gpp:dn:<DN>`), one per line in the header syntax of `kafka-console-producer --property parse.headers=true` (`key:::value` headers separated
by `,`, then `,,,` before the body), and a
gap report (JSON): every class and attribute of the tree with no home in smo-teiv's model, and how often.

    python spikes/teiv/load.py --synthetic 1000 --events /tmp/events.txt --gaps /tmp/gaps.json
"""

import argparse
import datetime
import importlib.util
import json
import random
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

# What smo-teiv 0.3.0 can hold (teiv/src/main/resources/models/*.yang at tag 0.3.0): the TS 28.541 class, the smo-teiv entity it maps to, and the
# attributes smo-teiv has for it. A class or an attribute not listed is a model gap.
CLASS_MAP: dict[str, tuple[str, set[str]]] = {
    "ManagedElement": ("o-ran-smo-teiv-oam:ManagedElement", set()),
    "GNBDUFunction": ("o-ran-smo-teiv-ran:ODUFunction", {"gNBDUId", "gNBId", "gNBIdLength"}),
    "GNBCUCPFunction": ("o-ran-smo-teiv-ran:OCUCPFunction", {"gNBCUName", "gNBId", "gNBIdLength"}),
    "NRCellDU": ("o-ran-smo-teiv-ran:NRCellDU", {"cellLocalId", "nCI", "nRPCI", "nRTAC"}),
    "NRCellCU": ("o-ran-smo-teiv-ran:NRCellCU", {"cellLocalId", "nCI", "nRTAC"}),
    "NRSectorCarrier": ("o-ran-smo-teiv-ran:NRSectorCarrier", {"arfcnDL", "arfcnUL", "frequencyDL", "frequencyUL", "bSChannelBwDL"}),
}
# Containment parent class -> child class -> the smo-teiv relationship that carries it (module:TYPE), when one exists.
CONTAINMENT: dict[tuple[str, str], str] = {
    ("ManagedElement", "GNBDUFunction"): "o-ran-smo-teiv-rel-oam-ran:MANAGEDELEMENT_MANAGES_ODUFUNCTION",
    ("ManagedElement", "GNBCUCPFunction"): "o-ran-smo-teiv-rel-oam-ran:MANAGEDELEMENT_MANAGES_OCUCPFUNCTION",
    ("GNBDUFunction", "NRCellDU"): "o-ran-smo-teiv-ran:ODUFUNCTION_PROVIDES_NRCELLDU",
    ("GNBDUFunction", "NRSectorCarrier"): "o-ran-smo-teiv-ran:ODUFUNCTION_PROVIDES_NRSECTORCARRIER",
    ("GNBCUCPFunction", "NRCellCU"): "o-ran-smo-teiv-ran:OCUCPFUNCTION_PROVIDES_NRCELLCU",
}
# A TS 28.541 reference attribute that is an edge in smo-teiv: NRCellDU.nRSectorCarrierRef -> NRCELLDU_USES_NRSECTORCARRIER.
REFERENCES: dict[tuple[str, str], str] = {("NRCellDU", "nRSectorCarrierRef"): "o-ran-smo-teiv-ran:NRCELLDU_USES_NRSECTORCARRIER"}
TEIV_ID = "urn:3gpp:dn:"
MAX_ENTITIES_PER_EVENT = 300            # keeps an event well under Kafka's default 1 MB message size


@dataclass
class Moi:
    """One managed object instance: its DN below the SubNetwork (the id smo-teiv keys it by), its full DN (with the SubNetwork, kept as a sourceId so a
    SubNetwork scope can still be asked for), its class, its parent's class and its attributes."""
    dn: str
    full_dn: str
    cls: str
    parent_cls: str | None
    attributes: dict = field(default_factory=dict)


def _child(dn: str, cls: str, ident: str) -> str:
    return f"{dn},{cls}={ident}" if dn else f"{cls}={ident}"


def synthetic(cells: int, subnetwork: str = "SubNetwork=Spike", per_gnb: int = 3, neighbours: int = 8, seed: int = 7) -> list[Moi]:
    """Source b): `cells` NRCellDU, `per_gnb` to a gNB; per cell an NRSectorCarrier (with a CommonBeamformingFunction) on the DU side and an NRCellCU
    with `neighbours` NRCellRelation (adjacentNRCellRef to cells of nearby gNBs) on the CU side. Attribute values are plausible, not real."""
    rnd = random.Random(seed)  # noqa: S311 — synthetic test data, not security
    out: list[Moi] = []
    gnbs = (cells + per_gnb - 1) // per_gnb
    cell_dns: list[str] = []
    for g in range(gnbs):
        me = f"ManagedElement=gNB-{g:04d}"
        out.append(Moi(me, f"{subnetwork},{me}", "ManagedElement", None, {"vendorName": "spike", "swVersion": "1.0"}))
        du = _child(me, "GNBDUFunction", "1")
        out.append(Moi(du, f"{subnetwork},{du}", "GNBDUFunction", "ManagedElement",
                       {"gNBDUId": g, "gNBId": 1000 + g, "gNBIdLength": 22, "gNBDUName": f"du-{g}", "administrativeState": "UNLOCKED"}))
        cu = _child(me, "GNBCUCPFunction", "1")
        out.append(Moi(cu, f"{subnetwork},{cu}", "GNBCUCPFunction", "ManagedElement",
                       {"gNBCUName": f"cu-{g}", "gNBId": 1000 + g, "gNBIdLength": 22, "pLMNId": {"mcc": "001", "mnc": "01"}}))
        for c in range(per_gnb):
            n = g * per_gnb + c
            if n >= cells:
                break
            sc = _child(du, "NRSectorCarrier", str(c + 1))
            out.append(Moi(sc, f"{subnetwork},{sc}", "NRSectorCarrier", "GNBDUFunction",
                           {"arfcnDL": 630000 + c, "arfcnUL": 630000 + c, "bSChannelBwDL": 100, "bSChannelBwUL": 100, "txDirection": "DL_AND_UL",
                            "configuredMaxTxPower": 40000}))
            bf = _child(sc, "CommonBeamformingFunction", "1")
            out.append(Moi(bf, f"{subnetwork},{bf}", "CommonBeamformingFunction", "NRSectorCarrier",
                           {"coverageShape": 1, "digitalAzimuth": 120 * c, "digitalTilt": 3}))
            cell = _child(du, "NRCellDU", str(c + 1))
            cell_dns.append(cell)
            out.append(Moi(cell, f"{subnetwork},{cell}", "NRCellDU", "GNBDUFunction",
                           {"cellLocalId": c + 1, "nCI": (1000 + g) * 16 + c, "nRPCI": n % 1008, "nRTAC": 100 + g % 50,
                            "administrativeState": rnd.choice(["UNLOCKED", "UNLOCKED", "LOCKED"]), "operationalState": "ENABLED",
                            "cellState": "ACTIVE", "ssbFrequency": 630000 + c, "pLMNInfoList": [{"mcc": "001", "mnc": "01", "sst": 1}],
                            "nRSectorCarrierRef": [sc]}))
            ccu = _child(cu, "NRCellCU", str(c + 1))
            out.append(Moi(ccu, f"{subnetwork},{ccu}", "NRCellCU", "GNBCUCPFunction", {"cellLocalId": c + 1, "nCI": (1000 + g) * 16 + c, "nRTAC": 100 + g % 50}))
    # neighbours: each cell's NRCellRelation points at `neighbours` cells of nearby gNBs (a ring, so two-hop neighbourhoods overlap as in a real grid)
    for n, cell in enumerate(cell_dns):
        cu_cell = cell.replace("GNBDUFunction=1,NRCellDU=", "GNBCUCPFunction=1,NRCellCU=")
        for k in range(1, neighbours + 1):
            target = cell_dns[(n + k * per_gnb + (k % per_gnb)) % len(cell_dns)]
            rel = _child(cu_cell, "NRCellRelation", str(k))
            out.append(Moi(rel, f"{subnetwork},{rel}", "NRCellRelation", "NRCellCU",
                           {"nRTCI": k, "adjacentNRCellRef": target, "isHOAllowed": True, "isRemoveAllowed": True, "cellIndividualOffset": {"rsrpOffsetSsb": 0}}))
    return out


def from_ran_nf_oam(url: str, elements: list[str], subnetwork: str = "SubNetwork=OranSim") -> list[Moi]:
    """Source a): the subtree RAN NF OAM's `managed_object` table holds for each element (GET /managed-objects/{root}/subtree, a nested `tree` with
    `children`). It carries classes and DNs, no attributes (the table has none, ADR 0008 section 3.6)."""
    out = []

    def walk(node: dict, parent_cls: str | None) -> None:
        dn = node["dn"]
        cls = node.get("class") or dn.rsplit(",", 1)[-1].split("=", 1)[0]
        out.append(Moi(dn, f"{subnetwork},{dn}", cls, parent_cls, {}))
        for child in node.get("children") or []:
            walk(child, cls)

    with httpx.Client(base_url=url.rstrip("/"), timeout=30) as client:
        for ref in elements:
            root = ref if ref.startswith("ManagedElement=") else f"ManagedElement={ref}"
            r = client.get(f"/managed-objects/{root}/subtree")
            r.raise_for_status()
            walk(r.json()["tree"], None)
    return out


def _xml_attributes(node) -> dict:
    """The children of a 3GPP YANG `attributes` container as a dict: leaves as text, repeated leaves and lists as lists of values or dicts."""
    out: dict = {}
    for child in node:
        name = child.tag.rsplit("}", 1)[-1]
        value = _xml_attributes(child) if len(child) else (child.text or "").strip()
        if name in out:
            out[name] = out[name] if isinstance(out[name], list) else [out[name]]
            out[name].append(value)
        else:
            out[name] = value
    return out


def from_netconf(uris: list[str], names: list[str] | None = None, subnetwork: str = "SubNetwork=OranSim", credential: str = "oransim") -> list[Moi]:
    """The simulators' ManagedElement as RAN NF OAM's NETCONF client reads it (get-config of `_3gpp-common-managed-element`), every list entry with an
    `id` and `attributes` taken as an MOI. The ManagedElement is named by `names[i]`, the element RAN NF OAM registered it as (its hostname), when
    given: every m-release simulator calls its own ManagedElement `ManagedElement-002`, so the simulator's id does not tell two elements apart."""
    import os
    import tempfile
    from urllib.parse import urlsplit

    from app.netconf_ssh import NetconfSession     # RAN NF OAM's client: PYTHONPATH=ran-nf-oam:shared, and the credential NETCONF_CRED_ORANSIM_PASSWORD
    out = []
    if not os.environ.get("NETCONF_SSH_KNOWN_HOSTS"):     # a spike against throwaway simulators: their host keys are pinned on first contact
        import socket

        import paramiko
        with tempfile.NamedTemporaryFile("w", suffix=".known_hosts", delete=False) as known:
            for uri in uris:
                u = urlsplit(uri)
                transport = paramiko.Transport(socket.create_connection((u.hostname, u.port or 830), timeout=15))
                transport.start_client(timeout=15)
                key = transport.get_remote_server_key()
                transport.close()
                known.write(f"[{u.hostname}]:{u.port or 830} {key.get_name()} {key.get_base64()}\n")
        os.environ["NETCONF_SSH_KNOWN_HOSTS"] = known.name
    for uri in uris:
        with NetconfSession(uri, timeout=30, credential_ref=credential) as s:
            reply = s.rpc('<rpc message-id="spike" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><get-config><source><running/></source>'
                          '<filter type="subtree"><ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element"/></filter></get-config></rpc>')

        element = names[uris.index(uri)] if names else None

        def walk(node, dn, parent_cls, element=element):
            for child in node:
                cls = child.tag.rsplit("}", 1)[-1]
                ident = next((c.text for c in child if c.tag.rsplit("}", 1)[-1] == "id"), None)
                if cls == "ManagedElement" and element:
                    ident = element
                attrs = next((c for c in child if c.tag.rsplit("}", 1)[-1] == "attributes"), None)
                if ident is None:
                    continue
                me_dn = _child(dn, cls, ident)
                out.append(Moi(me_dn, f"{subnetwork},{me_dn}", cls, parent_cls, _xml_attributes(attrs) if attrs is not None else {}))
                walk(child, me_dn, cls, element)
        data = next((n for n in reply.iter() if n.tag.rsplit("}", 1)[-1] == "data"), reply)
        walk(data, "", None)
    return out


def oran_sim_sources(logs: list[str]) -> tuple[list[str], list[str]]:
    """(elements, NETCONF URIs) of the oran-sim simulators whose logs are given: what their SSH pnfRegistration carried (scripts/oran_sim_check.py
    parses the log, and registers each element in RAN NF OAM under its hostname)."""
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("oran_sim_check", Path(__file__).resolve().parents[2] / "scripts" / "oran_sim_check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    elements, uris = [], []
    for path in logs:
        sim = check.parse_log(path)
        reg = sim.ssh_registration
        if sim.hostname and reg:
            extra = reg.get("additionalFields", {})
            elements.append(sim.hostname)
            uris.append(f"ssh://{extra.get('username', 'netconf')}@{reg['oamV4IpAddress']}:{extra.get('oamPort', 830)}")
    return elements, uris


def to_events(mois: list[Moi], source: str = "spike-loader") -> tuple[list[dict], dict]:
    """(events, gaps): the CloudEvent bodies for smo-teiv (entities and relationships, at most MAX_ENTITIES_PER_EVENT entities each, a relationship in
    the same event as its later end) and the model gaps: classes with no smo-teiv entity, attributes with no smo-teiv attribute, containments and
    references with no smo-teiv relationship, each with a count."""
    gaps: dict[str, Counter] = {"classes": Counter(), "attributes": Counter(), "relationships": Counter()}
    by_dn = {m.dn: m for m in mois}
    batches: list[dict] = []
    entities: dict[str, list] = {}
    relationships: dict[str, list] = {}
    count = 0

    def flush():
        nonlocal entities, relationships, count
        if count:
            batches.append({"entities": [{k: v} for k, v in entities.items()], "relationships": [{k: v} for k, v in relationships.items()]})
        entities, relationships, count = {}, {}, 0

    def rel(kind: str, a: str, b: str):
        relationships.setdefault(kind, []).append({"id": f"urn:oran:smo:teiv:{kind.split(':')[1]}={a};{b}", "aSide": TEIV_ID + a, "bSide": TEIV_ID + b,
                                                   "sourceIds": [TEIV_ID + a, TEIV_ID + b]})   # smo-teiv wants two

    for m in mois:
        if m.cls not in CLASS_MAP:
            gaps["classes"][m.cls] += 1
            continue
        teiv_type, known = CLASS_MAP[m.cls]
        attrs = {}
        for k, v in m.attributes.items():
            if (m.cls, k) in REFERENCES:
                for target in v if isinstance(v, list) else [v]:
                    if target in by_dn:
                        rel(REFERENCES[(m.cls, k)], m.dn, target)
            elif k in known:
                attrs[k] = int(v) if isinstance(v, str) and v.lstrip("-").isdigit() else v
            else:
                gaps["attributes"][f"{m.cls}.{k}"] += 1
        entity = {"id": TEIV_ID + m.dn, "sourceIds": [TEIV_ID + m.full_dn]}
        if attrs:
            entity["attributes"] = attrs
        entities.setdefault(teiv_type, []).append(entity)
        count += 1
        if m.parent_cls:
            parent_dn = m.dn.rsplit(",", 1)[0]
            kind = CONTAINMENT.get((m.parent_cls, m.cls))
            if kind:
                rel(kind, parent_dn, m.dn)
            elif m.parent_cls in CLASS_MAP:
                gaps["relationships"][f"{m.parent_cls} contains {m.cls}"] += 1
        if count >= MAX_ENTITIES_PER_EVENT:
            flush()
    flush()
    for m in mois:                                              # a neighbour is an NRCellRelation: smo-teiv has no relationship for it (counted once)
        if m.cls == "NRCellRelation":
            gaps["relationships"]["NRCellRelation (neighbour, NRCellCU -> adjacent NRCellDU / NRCellCU)"] += 1
    return batches, {k: dict(v) for k, v in gaps.items()}


def cloud_event_line(body: dict, ce_type: str = "ran-logical-topology.merge", source: str = "spike-loader") -> str:
    """One line for kafka-console-producer with parse.headers: the CloudEvents binary-mode headers, then the JSON body."""
    headers = {"ce_specversion": "1.0", "ce_id": str(uuid.uuid4()), "ce_source": source, "ce_type": ce_type, "content-type": "application/yang-data+json",
               "ce_time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "ce_dataschema": "https://teiv:8080/schemas/v1/r1-topology"}
    return ",".join(f"{k}:::{v}" for k, v in headers.items()) + ",,," + json.dumps(body, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    """The command line of the module docstring: read the sources, write the events file and the gap report, print a summary."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ran-nf-oam")
    p.add_argument("--element", action="append", default=[])
    p.add_argument("--netconf", action="append", default=[])
    p.add_argument("--oran-sim-log", action="append", default=[], help="an oran-sim simulator's log: its element (for --ran-nf-oam) and its NETCONF URI")
    p.add_argument("--synthetic", type=int, default=0)
    p.add_argument("--events", required=True, help="where to write the CloudEvent lines")
    p.add_argument("--gaps", help="where to write the gap report (JSON)")
    a = p.parse_args(argv)
    mois: list[Moi] = []
    if a.oran_sim_log:
        names, uris = oran_sim_sources(a.oran_sim_log)
        a.element += names
        a.netconf += uris
    if a.ran_nf_oam and a.element:
        mois += from_ran_nf_oam(a.ran_nf_oam, a.element)
    if a.netconf:
        mois += from_netconf(a.netconf, names if a.oran_sim_log else None)
    if a.synthetic:
        mois += synthetic(a.synthetic)
    merged: dict[str, Moi] = {}                       # one object reached through two sources (RAN NF OAM's root and the NETCONF read) is one MOI
    for m in mois:
        if m.dn in merged:
            merged[m.dn].attributes.update(m.attributes)
            merged[m.dn].parent_cls = merged[m.dn].parent_cls or m.parent_cls
        else:
            merged[m.dn] = m
    mois = list(merged.values())
    events, gaps = to_events(mois)
    with open(a.events, "w", encoding="utf-8") as out:
        for body in events:
            out.write(cloud_event_line(body) + "\n")
    report = {"mois": len(mois), "byClass": dict(Counter(m.cls for m in mois)), "events": len(events), "gaps": gaps}
    if a.gaps:
        with open(a.gaps, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=1)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
