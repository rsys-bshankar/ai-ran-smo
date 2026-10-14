"""PR-RAPP-1.3 (the trust store in compose and the chart) and PR-RAPP-2.3 (the rApp egress NetworkPolicy of the chart).

The first group reads the files and needs nothing else. The render tests need the `helm` binary and are skipped without it (CI's `helm` job
has it). The
fixtures and render helpers come from `test_helm_chart.py`. Run with: PYTHONPATH=shared python -m pytest tests_integration/test_helm_rapp.py
-q
"""

from test_helm_chart import CHART, COMPOSE, SMO_ROOT, VALUES, _deployment, _env_of, _modules, _pod_templates, _render, helm

# The chart's rApp modules: the four reference rApps and the workers that run their scheduled passes (call flows 22e, 23f), which are the same rApp identity.
RAPPS = {"energy-saving-rapp", "mobility-optimization-rapp", "coverage-optimization-rapp", "traffic-steering-rapp", "energy-saving-rapp-worker",
         "mobility-optimization-rapp-worker"}


def test_signing_and_the_egress_policy_are_off_by_default_in_the_chart_and_in_compose():
    """Package signing enforcement and the rApp egress policy are off by default in the chart, and compose passes the trust store and
    require-signed settings with empty and false defaults.
    """
    assert VALUES["rappSigning"] == {"trustStoreConfigMap": "", "requireSigned": False}
    assert VALUES["rappNetworkPolicy"]["enabled"] is False
    assert "ONBOARDING_TRUST_STORE" not in _modules()["onboarding"]["env"]                  # the chart sets them only when asked to
    env = COMPOSE["services"]["onboarding"]["environment"]
    assert env["ONBOARDING_TRUST_STORE"] == "${ONBOARDING_TRUST_STORE:-}" and env["ONBOARDING_REQUIRE_SIGNED_PACKAGES"] == "${ONBOARDING_REQUIRE_SIGNED_PACKAGES:-false}"


def test_the_chart_and_compose_name_the_signing_settings_the_code_reads():
    """The chart template sets the two environment variables Onboarding reads, mounts the trust store where the variable says, and the chart README
    documents both new value groups.
    """
    template = (CHART / "templates" / "modules.yaml").read_text()
    assert "name: ONBOARDING_TRUST_STORE" in template and "name: ONBOARDING_REQUIRE_SIGNED_PACKAGES" in template
    assert "mountPath: /run/rapp-trust" in template and "name: rapp-trust" in template
    assert "/run/rapp-trust" in template.split("name: ONBOARDING_TRUST_STORE")[1].splitlines()[0]          # the env names the path that is mounted
    onboarding = (SMO_ROOT / "onboarding" / "app" / "main.py").read_text()
    assert "ONBOARDING_TRUST_STORE" in onboarding and "ONBOARDING_REQUIRE_SIGNED_PACKAGES" in onboarding
    readme = (CHART / "README.md").read_text()
    assert "rappSigning" in readme and "rappNetworkPolicy" in readme


def test_the_egress_policy_selects_the_rapps_by_the_identity_kind_the_chart_gives_them_and_adds_no_pod_label():
    """The egress policy template selects rApps by the identity kind the chart already sets and adds no pod label, since a label would change the
    pod template and restart the rApps.
    """
    assert {n for n, m in _modules().items() if m["env"].get("SMO_IDENTITY_KIND") == "rapp"} == RAPPS
    template = (CHART / "templates" / "rapp-egress.yaml").read_text()
    assert 'get $m.env "SMO_IDENTITY_KIND"' in template and 'include "smo.podLabels"' not in template and "template:" not in template and "kind: NetworkPolicy" in template
    assert "policyTypes: [Egress]" in template
    assert "pod template" in template              # the rule that keeps a new label out of the pods is written where someone adding one will read it


@helm
def test_the_rapp_egress_policy_is_not_rendered_by_default():
    """By default the chart renders no rApp egress policy. Needs helm."""
    assert not [d for d in _render() if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].startswith("rapp-egress")]


def _egress_policies(*args):
    return [d for d in _render("--set", "rappNetworkPolicy.enabled=true", *args) if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].startswith("rapp-egress")]


@helm
def test_the_rapp_egress_policy_lets_the_reference_rapps_reach_r1_dns_and_the_database_only():
    """The rendered policy selects the reference rApps (and their workers) and allows egress to the gateway on 8000, to cluster DNS on 53 and to the database on
    5432, and nothing else. Needs helm.
    """
    [policy] = _egress_policies()
    assert policy["metadata"]["name"] == "rapp-egress-0" and policy["spec"]["policyTypes"] == ["Egress"]
    selector = policy["spec"]["podSelector"]
    assert selector["matchLabels"] == {"app.kubernetes.io/instance": "smo"}
    [expression] = selector["matchExpressions"]
    assert expression["key"] == "app.kubernetes.io/name" and expression["operator"] == "In" and set(expression["values"]) == RAPPS
    r1, dns, database = policy["spec"]["egress"]
    assert r1["to"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "smo", "app.kubernetes.io/name": "r1-termination"}}}]
    assert r1["ports"] == [{"protocol": "TCP", "port": 8000}]
    assert {(p["protocol"], p["port"]) for p in dns["ports"]} == {("UDP", 53), ("TCP", 53)} and dns["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
    assert database["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "postgres" and database["ports"] == [{"protocol": "TCP", "port": 5432}]


@helm
def test_the_egress_policy_changes_no_pod_template_so_it_restarts_nothing():
    """Turning the policy on, with or without extra selectors, changes no pod template, so it restarts nothing. Needs helm."""
    plain = _pod_templates(_render())
    assert plain == _pod_templates(_render("--set", "rappNetworkPolicy.enabled=true"))
    assert plain == _pod_templates(_render("--set", "rappNetworkPolicy.enabled=true", "--set", "rappNetworkPolicy.extraPodSelectors[0].matchLabels.app=mine"))


@helm
def test_the_egress_policy_options_drop_or_add_rules():
    """The options drop the database and DNS rules, drop the database rule for an external Postgres, add Tempo when the bundled one is on (and not
    when the tracing endpoint is the operator's own), and append extra egress rules. Needs helm.
    """
    def pods(policy):
        labels = [r["to"][0]["podSelector"]["matchLabels"] for r in policy["spec"]["egress"] if "to" in r and "podSelector" in r["to"][0]]
        return [m["app.kubernetes.io/name"] for m in labels if "app.kubernetes.io/name" in m]       # the DNS rule selects kube-dns by k8s-app, not by this label
    [bare] = _egress_policies("--set", "rappNetworkPolicy.database=false", "--set", "rappNetworkPolicy.dns=false")
    assert pods(bare) == ["r1-termination"] and len(bare["spec"]["egress"]) == 1
    [external_db] = _egress_policies("--set", "postgres.enabled=false", "--set", "postgres.external.host=db.example.com")
    assert "postgres" not in str(external_db)
    [traced] = _egress_policies("--set", "observability.tempo.enabled=true")
    assert pods(traced)[-1] == "tempo"
    [own_collector] = _egress_policies("--set", "observability.tempo.enabled=true", "--set", "tracing.endpoint=http://collector.obs:4318")
    assert "tempo" not in str(own_collector)
    [extra] = _egress_policies("--set", "rappNetworkPolicy.extraEgress[0].to[0].ipBlock.cidr=10.20.0.0/24", "--set", "rappNetworkPolicy.extraEgress[0].ports[0].port=5432")
    assert extra["spec"]["egress"][-1] == {"to": [{"ipBlock": {"cidr": "10.20.0.0/24"}}], "ports": [{"port": 5432}]}


@helm
def test_a_rapp_workload_of_your_own_gets_a_policy_of_its_own():
    """An rApp of the operator's own, named by an extra pod selector, gets its own numbered policy allowing the gateway. Needs helm."""
    policies = _egress_policies("--set", "rappNetworkPolicy.extraPodSelectors[0].matchLabels.app=my-rapp")
    assert [p["metadata"]["name"] for p in policies] == ["rapp-egress-0", "rapp-egress-1"]
    assert policies[1]["spec"]["podSelector"] == {"matchLabels": {"app": "my-rapp"}}
    assert policies[1]["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 8000}]


@helm
def test_with_every_chart_rapp_disabled_only_the_extra_selectors_get_a_policy():
    """With every chart rApp disabled no policy is rendered, and an extra selector alone gets one. Needs helm."""
    args = [x for n in sorted(RAPPS) for x in ("--set", f"modules.{n}.enabled=false")]
    assert _egress_policies(*args) == []
    policies = _egress_policies(*args, "--set", "rappNetworkPolicy.extraPodSelectors[0].matchLabels.app=mine")
    assert [p["metadata"]["name"] for p in policies] == ["rapp-egress-0"] and policies[0]["spec"]["podSelector"] == {"matchLabels": {"app": "mine"}}


@helm
def test_the_trust_store_configmap_is_mounted_and_named_only_when_set_and_nothing_else_changes():
    """The trust store ConfigMap is mounted read-only into Onboarding, with the path and require-signed variables, only when
    `rappSigning.trustStoreConfigMap` is set, and no other module gets the setting. Needs helm.
    """
    off = _deployment(_render(), "onboarding")["spec"]["template"]["spec"]
    assert "ONBOARDING_TRUST_STORE" not in {e["name"] for e in off["containers"][0]["env"]} and "rapp-trust" not in {v["name"] for v in off["volumes"]}
    docs = _render("--set", "rappSigning.trustStoreConfigMap=rapp-publishers", "--set", "rappSigning.requireSigned=true")
    on = _deployment(docs, "onboarding")["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in on["containers"][0]["env"]}
    assert env["ONBOARDING_TRUST_STORE"] == "/run/rapp-trust" and env["ONBOARDING_REQUIRE_SIGNED_PACKAGES"] == "true"
    assert {"name": "rapp-trust", "mountPath": "/run/rapp-trust", "readOnly": True} in on["containers"][0]["volumeMounts"]
    assert next(v for v in on["volumes"] if v["name"] == "rapp-trust")["configMap"]["name"] == "rapp-publishers"
    for name in set(_modules()) - {"onboarding"}:
        assert _env_of(_deployment(docs, name)).get("ONBOARDING_TRUST_STORE") is None, name
