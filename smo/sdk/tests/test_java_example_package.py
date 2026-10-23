"""The Java SDK's example rApp package (`sdk-java/examples/hello-rapp/package/`) builds into a CSAR that passes the checks Onboarding makes.

The Java side (`sdk-java/`) has its own tests; this file holds the half that needs the platform's Python code: the package is built with `samples/build_csar.py --source-dir` and its `operatorUi`
goes through `smo_sdk.operator_ui` (the code Onboarding runs). Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_java_example_package.py -q`; needs the repository checkout
(`sdk-java/` and `samples/`), no network.
"""

import importlib.util
import io
import zipfile
from pathlib import Path

import yaml

from smo_sdk import operator_ui as ui

SMO = Path(__file__).resolve().parents[2]
PACKAGE = SMO / "sdk-java" / "examples" / "hello-rapp" / "package"


def _builder():
    """`samples/build_csar.py` loaded by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("build_csar", SMO / "samples" / "build_csar.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csar() -> bytes:
    return _builder().build_bytes("hello-java-rapp", source=PACKAGE)


def test_the_package_has_the_files_onboarding_reads_and_rebuilds_byte_identically():
    """The CSAR has exactly the files Onboarding reads, the ASD names the rApp, and two builds of the same sources are byte-identical."""
    with zipfile.ZipFile(io.BytesIO(_csar())) as z:
        assert sorted(z.namelist()) == ["Definitions/asd.yaml", "TOSCA-Metadata/DIGESTS.sha256", "TOSCA-Metadata/DIGESTS.sha256.sig", "TOSCA-Metadata/TOSCA.meta", "capabilities.yaml",
                                        "manifest.yaml"]
        assert "Entry-Definitions: Definitions/asd.yaml" in z.read("TOSCA-Metadata/TOSCA.meta").decode()
        asd = z.read("Definitions/asd.yaml").decode()
    for key in ("application_name: HelloJava_rApp", "application_version: \"1.0.0\"", "provider:", "descriptor_id:", "descriptor_invariant_id:", "schema_version"):
        assert key in asd
    assert _csar() == _csar()


def test_the_manifest_declares_valid_modes_profiles_and_an_operator_page_the_java_rapp_serves():
    """Every runtime profile belongs to a declared execution mode and the operator page passes Onboarding's check with exactly the three routes the Java example serves."""
    with zipfile.ZipFile(io.BytesIO(_csar())) as z:
        manifest = yaml.safe_load(z.read("manifest.yaml"))
    assert manifest["rappManifest"]["manifestVersion"] == "1.0"
    assert set(manifest["runtimeProfiles"]) <= set(manifest["executionModes"])
    declared = ui.validate(manifest["operatorUi"])
    # exactly the routes io.smo.example.OperatorServer serves under the operator API base
    assert set(ui.declared_routes(declared)) == {("GET", "/instances/{instanceId}/status"), ("GET", "/instances/{instanceId}/runs"),
                                                 ("POST", "/instances/{instanceId}/run")}


def test_the_samples_own_build_is_unchanged_by_the_source_dir_option():
    """Adding the `source` option to `build_bytes` did not change how a sample is built: the energy-saving sample still equals its committed CSAR."""
    builder = _builder()
    assert builder.build_bytes("energy-saving-rapp") == (SMO / "samples" / "energy-saving-rapp.csar").read_bytes()


def test_the_package_declares_the_namespaces_the_java_rapp_calls_and_the_smallest_limits():
    """`capabilities.yaml` consumes exactly `data` and `models` (the example calls `data().listTypes` and `models().listModels`) and provides nothing, and the manifest's
    `limits` are the smallest Onboarding accepts, because the example writes no configuration."""
    with zipfile.ZipFile(io.BytesIO(_csar())) as z:
        capabilities = yaml.safe_load(z.read("capabilities.yaml"))["capabilities"]
        manifest = yaml.safe_load(z.read("manifest.yaml"))
    assert [c["namespace"] for c in capabilities["consumes"]] == ["data", "models"] and capabilities["provides"] == []
    source = (SMO / "sdk-java" / "examples" / "hello-rapp" / "src" / "main" / "java" / "io" / "smo" / "example" / "HelloRapp.java").read_text()
    assert "sdk.data().listTypes" in source and "sdk.models().listModels" in source and "mediateAction" not in source
    assert manifest["limits"] == {"configJobsPerHour": 1, "maxElementsPerJob": 1}
