"""Tests for the O-RAN WG10 TE&IV R1 API in specs/ (plan task S3.24): specs/oran/teiv/TEIV_API_R005_v04.yaml is O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00's
Annex A (OpenAPI 3.0.3, info.version 1.2.1, 16 operations under /topology-inventory/v1), the bundle's teiv.yaml lists each of its operations with its
mapping onto smo-teiv 0.3.0 and a path $ref'd into the file, and, when the PDF is at hand, the file is exactly what specs/oran/teiv/extract_api.py extracts
from it and states the version the PDF states."""

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "specs" / "oran" / "teiv" / "TEIV_API_R005_v04.yaml"
CONTRACT = REPO / "specs" / "smo-schema-bundle" / "openapi" / "modules" / "teiv.yaml"
DOC = "O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00"
VERSION = "1.2.1"            # Annex A, A.2: "version: 1.2.1"
PDF = Path(os.environ.get("TEIV_API_PDF") or REPO.parent / "smo-converged-inputs" / "oran-specs" / "O-RAN.WG10.TS.TEIV-API.0-R005-v04.00.pdf")
METHODS = ("get", "put", "post", "patch", "delete")


def _spec() -> dict:
    """The extracted OpenAPI document."""
    return yaml.safe_load(SPEC.read_text())


def _operations(doc: dict) -> dict[str, tuple[str, str]]:
    """operationId -> (METHOD, path) of every operation of `doc`."""
    return {op["operationId"]: (m.upper(), p) for p, item in doc["paths"].items() for m, op in item.items() if m in METHODS}


def test_the_file_is_annex_a_of_the_named_version():
    """OpenAPI 3.0.3, info.version 1.2.1, the document named in externalDocs, the server base /topology-inventory/v1, 16 operations."""
    doc = _spec()
    assert doc["openapi"] == "3.0.3" and doc["info"]["version"] == VERSION
    assert doc["externalDocs"]["description"] == DOC
    assert doc["servers"][0]["url"] == "{apiRoot}/topology-inventory/v1"
    assert len(_operations(doc)) == 16


def test_teiv_yaml_lists_every_operation_once_with_its_mapping_and_a_ref_into_the_file():
    """Each of the 16 operations in x-smo-teiv-api, same method and path, mapped `same` or `differs` (a `differs` says how), and each path of the file is a
    path of the contract under /topology-inventory/v1 that $refs it."""
    contract = yaml.safe_load(CONTRACT.read_text())
    ops = _operations(_spec())
    listed = {o["operationId"]: o for o in contract["x-smo-teiv-api"]["operations"]}
    assert set(listed) == set(ops)
    for op_id, entry in listed.items():
        assert (entry["method"], entry["path"]) == ops[op_id]
        assert entry["mapping"] in ("same", "differs") and (entry["mapping"] == "same" or entry.get("note"))
    for path in _spec()["paths"]:
        pointer = path.replace("~", "~0").replace("/", "~1")
        assert contract["paths"][f"/topology-inventory/v1{path}"] == {"$ref": f"../../../oran/teiv/TEIV_API_R005_v04.yaml#/paths/{pointer}"}


@pytest.mark.skipif(not PDF.exists(), reason=f"the specification PDF is not at {PDF} (set TEIV_API_PDF): it is O-RAN's, behind a licence click-through")
def test_the_pdf_states_the_version_and_the_file_is_its_extraction():
    """The PDF's own text says `version: 1.2.1` in Annex A, and extract_api.py run on it writes this file byte for byte."""
    text = subprocess.run(["pdftotext", "-layout", str(PDF), "-"], capture_output=True, text=True, check=True).stdout  # noqa: S603,S607
    annex = text[text.index("A.2            Topology & Inventory API"):]
    assert re.search(r"^\s*version:\s*(\S+)", annex, re.M).group(1) == VERSION
    spec = importlib.util.spec_from_file_location("extract_api", SPEC.with_name("extract_api.py"))
    extract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extract)
    assert extract.extract(PDF)[0] == SPEC.read_text()
