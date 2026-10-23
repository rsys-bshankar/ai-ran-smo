"""PR-SEC-9.2 / PR-SEC-8.5 in the Helm chart: /bootstrap exposure (a NetworkPolicy and a per-path ingress rule, both off by default) and the shared limiter's setting.

The text tests read the chart's files and need no helm; the render tests need the `helm` binary and are skipped without it (CI's `helm` job has it).
"""

import json

from test_helm_chart import CHART, COMPOSE, VALUES, _compose_services, _render, helm

TEMPLATES = CHART / "templates"
SOURCES = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "rapps"}}}]


def test_the_bootstrap_network_policy_is_off_by_default_and_the_template_is_gated_on_it():
    """The bootstrap NetworkPolicy is off by default, its template is wholly inside the switch with its blocks balanced, and it selects the gateway
    on the gateway's own port, since a policy cannot name a URL path.
    """
    assert VALUES["bootstrapNetworkPolicy"] == {"enabled": False, "allowedSources": []}
    text = (TEMPLATES / "networkpolicy.yaml").read_text()
    body = text.split("*/}}", 1)[1].strip()                                           # after the leading comment
    assert body.startswith("{{- if .Values.bootstrapNetworkPolicy.enabled }}") and body.endswith("{{- end }}")     # the whole object is inside the switch
    assert body.count("{{- end }}") == body.count("{{- if ") + body.count("{{- with ")
    assert "kind: NetworkPolicy" in body and "app.kubernetes.io/name: r1-termination" in body
    assert "policyTypes: [Ingress]" in body and ".Values.bootstrapNetworkPolicy.allowedSources" in body
    assert "port: {{ $m.port }}" in body                                               # the gateway's port, per pod: a policy cannot name a path
    assert VALUES["moduleDefaults"]["port"] == 8000 and "port" not in VALUES["modules"]["r1-termination"]


def test_the_template_says_a_network_policy_is_not_per_path_and_where_the_per_path_rule_is():
    """The template's comment says a network policy cannot restrict URL paths and points to the per-path ingress rule and the gateway's bootstrap
    key.
    """
    text = (TEMPLATES / "networkpolicy.yaml").read_text()
    assert "never URL paths" in text and "bootstrapAllowedSourceRanges" in text and "R1_BOOTSTRAP_KEY" in text


def test_the_per_path_ingress_rule_is_off_by_default_and_exact_when_on():
    """The per-path ingress rule is off by default, and when on it is an exact `/bootstrap` path with a source whitelist."""
    assert VALUES["ingress"]["r1"]["bootstrapAllowedSourceRanges"] == [] and VALUES["ingress"]["enabled"] is False
    text = (TEMPLATES / "ingress.yaml").read_text()
    assert "{{- if and .host .bootstrapAllowedSourceRanges }}" in text
    assert "path: /bootstrap" in text and "pathType: Exact" in text and "nginx.ingress.kubernetes.io/whitelist-source-range" in text
    assert text.count("{{- end }}") == text.count("{{- if ") + text.count("{{- with ") + text.count("{{- range ")


def test_the_gateway_settings_this_change_adds_are_the_same_in_compose_and_the_chart():
    """The shared-limiter store and the bootstrap key have the same defaults in compose and the chart, and every service that calls the gateway can
    present the key.
    """
    env = COMPOSE["services"]["r1-termination"]["environment"]
    assert env["R1_RATE_STORE"] == "${R1_RATE_STORE:-memory}" and VALUES["modules"]["r1-termination"]["env"]["R1_RATE_STORE"] == "memory"
    assert env["R1_BOOTSTRAP_KEY"] == "${R1_BOOTSTRAP_KEY:-}"                          # empty: off
    # DOC-CF-3.1: a module reaches the gateway through R1Client's default URL without naming R1_GATEWAY_URL, and the GUI BFF through its own client, so
    # every service that is an SMO module (it mounts the enrollment secret) is a caller too, but SME and FOCOM, which never ask /bootstrap
    callers = [name for name, service in _compose_services().items()
               if {"R1_GATEWAY_URL", "SMO_ENROLLMENT_SECRET_FILE"} & set(service.get("environment") or {}) and name not in ("sme", "focom", "r1-termination")]
    assert {"dme", "dme-worker", "onboarding", "mlmr", "mdaf", "mdaf-worker", "intent-service", "gui-bff"} <= set(callers)
    for name in callers:                                                                  # every module and rApp that calls the gateway can present the key
        assert COMPOSE["services"][name]["environment"]["SMO_BOOTSTRAP_KEY"] == "${R1_BOOTSTRAP_KEY:-}", name


@helm
def test_the_default_install_has_no_network_policy_and_no_bootstrap_ingress():
    """A default install, even with the ingress on, has no NetworkPolicy and only the one gateway Ingress. Needs helm."""
    docs = _render("--set", "ingress.enabled=true", "--set", "ingress.r1.host=r1.example.com")
    assert not [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert [d["metadata"]["name"] for d in docs if d["kind"] == "Ingress"] == ["r1"]


@helm
def test_the_bootstrap_network_policy_limits_the_gateway_pods_ingress_to_the_release_and_the_listed_sources():
    """With the policy on, the gateway pods accept TCP on 8000 only from the release's own pods and the listed sources. Needs helm."""
    policy = next(d for d in _render("--set", "bootstrapNetworkPolicy.enabled=true", "--set-json", f"bootstrapNetworkPolicy.allowedSources={json.dumps(SOURCES)}")
                  if d["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "r1-termination"
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]
    assert rule["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/part-of"] == "smo"
    assert rule["from"][1:] == SOURCES


@helm
def test_the_bootstrap_network_policy_without_extra_sources_allows_only_the_release():
    """With no extra sources the policy allows only the release's own pods. Needs helm."""
    policy = next(d for d in _render("--set", "bootstrapNetworkPolicy.enabled=true") if d["kind"] == "NetworkPolicy")
    assert len(policy["spec"]["ingress"][0]["from"]) == 1


@helm
def test_bootstrap_source_ranges_add_an_exact_path_ingress_with_the_whitelist():
    """Source ranges add a second Ingress for the exact `/bootstrap` path with the whitelist annotation, the same class and TLS Secret, and leave
    the rest of the host unrestricted. Needs helm.
    """
    docs = _render("--set", "ingress.enabled=true", "--set", "ingress.r1.host=r1.example.com", "--set", "ingress.r1.tlsSecretName=r1-tls",
                   "--set", "ingress.className=nginx", "--set-json", 'ingress.r1.bootstrapAllowedSourceRanges=["10.0.0.0/8","192.168.1.0/24"]')
    ingresses = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Ingress"}
    assert set(ingresses) == {"r1", "r1-bootstrap"}
    bootstrap = ingresses["r1-bootstrap"]
    assert bootstrap["metadata"]["annotations"]["nginx.ingress.kubernetes.io/whitelist-source-range"] == "10.0.0.0/8,192.168.1.0/24"
    (path,) = bootstrap["spec"]["rules"][0]["http"]["paths"]
    assert path["path"] == "/bootstrap" and path["pathType"] == "Exact" and path["backend"]["service"]["name"] == "r1-termination"
    assert bootstrap["spec"]["ingressClassName"] == "nginx" and bootstrap["spec"]["tls"][0]["secretName"] == "r1-tls"
    assert "nginx.ingress.kubernetes.io/whitelist-source-range" not in (ingresses["r1"]["metadata"].get("annotations") or {})     # the rest of the host is unchanged


@helm
def test_the_shared_limiter_setting_reaches_the_gateway_pod():
    """`modules.r1-termination.env.R1_RATE_STORE` reaches the gateway's container environment. Needs helm."""
    docs = _render("--set", "modules.r1-termination.env.R1_RATE_STORE=postgres")
    r1 = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "r1-termination")
    assert {"name": "R1_RATE_STORE", "value": "postgres"} in r1["spec"]["template"]["spec"]["containers"][0]["env"]


@helm
def test_the_bootstrap_key_from_a_secret_reaches_the_gateway_and_every_caller():
    """DOC-CF-3.2: with `bootstrapKeySecretRef` set, the gateway gets R1_BOOTSTRAP_KEY and every other module (the GUI backend and the workers included) gets
    SMO_BOOTSTRAP_KEY, both from that Secret; the chart passed it to nothing before. Needs helm."""
    docs = _render("--set", "bootstrapKeySecretRef.name=r1-bootstrap")
    envs = _envs_of(docs)
    assert envs["r1-termination"]["R1_BOOTSTRAP_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "r1-bootstrap", "key": "bootstrap-key"}
    for name in ("dme", "dme-worker", "onboarding", "mlmr", "mdaf", "intent-service", "gui-bff"):
        assert envs[name]["SMO_BOOTSTRAP_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "r1-bootstrap", name
    assert not any("SMO_BOOTSTRAP_KEY" in e for e in _envs_of(_render()).values())


def _envs_of(docs) -> dict[str, dict]:
    """Deployment name -> its containers' environment by variable name."""
    return {d["metadata"]["name"]: {e["name"]: e for c in d["spec"]["template"]["spec"]["containers"] for e in c.get("env") or []}
            for d in docs if d and d["kind"] == "Deployment"}
