"""PR-SEC-4.7: the External Secrets example (deploy/external-secrets) names only Secrets and keys the Helm chart really reads.

It needs nothing but PyYAML: the example is parsed and compared with the chart's values.yaml and templates, so renaming a key in the chart
fails here instead of leaving an example that creates a Secret nobody reads. It does not apply anything: the example has not been applied
to a cluster (README.md in the directory says so, and a test below keeps it saying so).
"""

import re
from pathlib import Path

import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = SMO_ROOT / "deploy" / "external-secrets"
CHART = SMO_ROOT / "deploy" / "helm" / "smo"
CHART_VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
TEMPLATES = {p.name: p.read_text() for p in (CHART / "templates").glob("*")}

STORE = yaml.safe_load((EXAMPLE / "secret-store-vault.yaml").read_text())
EXTERNAL = [d for d in yaml.safe_load_all((EXAMPLE / "external-secrets.yaml").read_text()) if d]
VALUES = yaml.safe_load((EXAMPLE / "values.yaml").read_text())


def _targets():
    return {d["spec"]["target"]["name"]: [e["secretKey"] for e in d["spec"]["data"]] for d in EXTERNAL}


def _roles():
    return {m["databaseRole"] for m in CHART_VALUES["modules"].values() if m.get("databaseRole")}


def _walk(node):
    """Yields a parsed YAML node and every dict and list nested in it, depth first."""
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_every_document_is_an_external_secret_pointing_at_the_store_that_is_defined():
    """Every example document is an ExternalSecret in the chart's namespace that points at the example's ClusterSecretStore, owns its target Secret
    and names a remote key and property for each entry.
    """
    assert STORE["kind"] == "ClusterSecretStore"
    store = STORE["metadata"]["name"]
    assert EXTERNAL
    for d in EXTERNAL:
        assert d["kind"] == "ExternalSecret", d["metadata"]["name"]
        assert d["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": store}
        assert d["metadata"]["namespace"] == "smo"          # the chart's namespace (deploy/gitops/base)
        assert d["spec"]["target"]["creationPolicy"] == "Owner"
        for e in d["spec"]["data"]:
            assert e["remoteRef"]["key"] and e["remoteRef"]["property"]


def test_the_example_values_only_set_keys_the_chart_has():
    """Every key the example's values file sets exists in the chart's values.yaml, so a renamed chart key fails here."""
    def check(example, chart, path=""):
        for k, v in example.items():
            assert k in chart, f"{path}{k} is not in the chart's values.yaml"
            if isinstance(v, dict) and v and isinstance(chart[k], dict) and chart[k]:
                check(v, chart[k], f"{path}{k}.")
    check(VALUES, CHART_VALUES)


def test_the_owner_secret_has_the_name_and_keys_the_chart_mounts():
    """The example's owner Secret has the name and the two keys (database password and enrollment secret) that the chart's templates mount,
    matching the chart's own generated Secret.
    """
    name = VALUES["secrets"]["existingSecret"]
    assert 'default "smo-secrets"' in TEMPLATES["_helpers.tpl"] and name == "smo-secrets"
    mounted = set(re.findall(r"key: ([a-z-]+), path: (?:db_password|enrollment_secret)\b", TEMPLATES["modules.yaml"]))
    assert mounted == {"db-password", "enrollment-secret"}
    assert set(_targets()[name]) == mounted
    # the chart's own generated Secret has the same keys (migrate.yaml and postgres.yaml read the same Secret)
    assert all(f"{k}:" in TEMPLATES["secrets.yaml"] for k in mounted)


def test_the_role_secret_has_one_key_per_database_role_and_no_other():
    """The role Secret has one `db-password-<role>` key per database role and no other, and the chart builds its key names the same way."""
    name = VALUES["databaseRoles"]["existingSecret"]
    assert 'default "smo-role-secrets"' in TEMPLATES["_helpers.tpl"] and name == "smo-role-secrets"
    assert 'printf "db-password-%s"' in TEMPLATES["modules.yaml"]
    assert set(_targets()[name]) == {f"db-password-{r}" for r in _roles()}
    assert len(_targets()[name]) == len(set(_targets()[name]))


def test_the_gui_secret_references_name_a_secret_and_key_the_example_creates_and_the_chart_reads():
    """The OIDC client secret and TOTP key references name a Secret and key the example creates, and the chart reads the same variable with the
    same default key.
    """
    for ref, var, default in (("oidcClientSecretRef", "GUI_OIDC_CLIENT_SECRET", "client-secret"), ("totpKeySecretRef", "GUI_TOTP_KEY", "totp-key")):
        value = VALUES["gui"][ref]
        assert value["key"] in _targets()[value["name"]]
        assert f"name: {var}" in TEMPLATES["modules.yaml"]
        assert f'default "{default}"' in TEMPLATES["modules.yaml"]
        assert ref in CHART_VALUES["gui"]


def test_the_identity_key_reference_names_a_secret_and_key_the_example_creates_and_the_chart_reads():
    """SEC-15.18: `identityKeySecretRef` names the Secret and key the example creates, and the chart passes it as SMO_IDENTITY_KEY with the same default key."""
    value = VALUES["identityKeySecretRef"]
    assert value["key"] in _targets()[value["name"]]
    assert "name: SMO_IDENTITY_KEY" in TEMPLATES["modules.yaml"] and 'default "identity-key"' in TEMPLATES["modules.yaml"]
    assert "identityKeySecretRef" in CHART_VALUES


def test_the_mtls_secret_is_named_and_keyed_as_the_chart_reads_it():
    """The per-module mTLS Secrets use the chart's suffix, belong to a chart module and carry exactly `tls.crt`, `tls.key` and `ca.crt`."""
    suffix = CHART_VALUES["mtls"]["secretSuffix"]
    assert "tls.crt, tls.key, ca.crt" in (CHART / "values.yaml").read_text()
    found = [d for d in EXTERNAL if d["spec"]["target"]["name"].endswith(suffix)]
    assert found
    for d in found:
        module = d["spec"]["target"]["name"][: -len(suffix)]
        assert module in CHART_VALUES["modules"]
        assert sorted(e["secretKey"] for e in d["spec"]["data"]) == ["ca.crt", "tls.crt", "tls.key"]


def test_every_secret_the_example_creates_is_read_by_the_chart_through_the_values_or_the_mtls_name():
    """Every Secret the example creates is read by the chart, through a values reference or the mTLS name, so the example creates nothing nobody
    uses.
    """
    referenced = {VALUES["secrets"]["existingSecret"], VALUES["databaseRoles"]["existingSecret"],
                  VALUES["gui"]["oidcClientSecretRef"]["name"], VALUES["gui"]["totpKeySecretRef"]["name"], VALUES["identityKeySecretRef"]["name"]}
    suffix = CHART_VALUES["mtls"]["secretSuffix"]
    for name in _targets():
        assert name in referenced or name.endswith(suffix), f"{name} is created but nothing in the chart reads it"


def test_no_secret_value_is_in_the_example():
    """The example holds no secret value: no Secret document, no credential field, no PEM block and nothing that looks like an inline password,
    secret or token.
    """
    for path in EXAMPLE.glob("*.yaml"):
        text = path.read_text()
        for doc in yaml.safe_load_all(text):
            for node in _walk(doc):
                if isinstance(node, dict):
                    assert node.get("kind") != "Secret", f"{path.name} creates a Secret directly"
                    assert not {"stringData", "token", "password", "secretId", "tokenSecretRef"} & set(node), path.name
        assert "BEGIN " not in text                                              # no PEM block
        assert not re.search(r"(?i)\b(password|secret|token)\s*:\s*[\"']?[A-Za-z0-9+/=]{16,}", text), path.name


def test_the_readme_says_plainly_that_it_has_not_been_applied_to_a_cluster():
    """The example's README keeps saying it has not been applied to a cluster."""
    text = (EXAMPLE / "README.md").read_text()
    assert "NOT been applied to a cluster" in text
