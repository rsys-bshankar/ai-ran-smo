"""Echo's configuration (app/main.py): ECHO_PLACEMENT and ECHO_MSAC_RULES are read on every request and fail closed when they cannot be read.

Run with: cd smo/echo && PYTHONPATH=.:../shared python -m pytest tests/test_settings.py -q
"""

import pytest

from app import main
from smo_shared import msac
from smo_shared.mns.checks import Caller

ALICE = Caller(role="rapp", invoker="alice")


def test_placement_by_prefix_and_nowhere_without_a_setting(monkeypatch):
    """A DN under a configured prefix is placed there; without the setting it is placed nowhere."""
    monkeypatch.setenv("ECHO_PLACEMENT", '{"SubNetwork=A": {"region": "eu", "tenant": "t1", "siteCluster": "sc"}}')
    target = main.placement("SubNetwork=A,ManagedElement=1")
    assert (target.region, target.tenant, target.site_cluster) == ("eu", "t1", "sc")
    monkeypatch.delenv("ECHO_PLACEMENT")
    assert main.placement("SubNetwork=A").region is None


@pytest.mark.parametrize("value", ["not json", "[1, 2]", '"x"'])
def test_placement_that_cannot_be_read_places_nothing(monkeypatch, value):
    """Invalid JSON, or JSON that is not an object, places nothing (so a scoped caller reaches nothing it could not before)."""
    monkeypatch.setenv("ECHO_PLACEMENT", value)
    target = main.placement("SubNetwork=A")
    assert (target.region, target.tenant, target.site_cluster) == (None, None, None)


def test_rules_of_a_listed_caller_and_none_for_an_unlisted_one(monkeypatch):
    """A listed caller gets its rules; one not listed is not managed (None); no setting manages nobody."""
    monkeypatch.setenv("ECHO_MSAC_RULES", '{"alice": [{"dataNodeSelector": "/SubNetwork=A/*", "operations": ["read"], "action": "ALLOW"}]}')
    assert main.msac_rules(ALICE) == [msac.Rule("/SubNetwork=A/*", frozenset({"read"}), "ALLOW")]
    assert main.msac_rules(Caller(role="rapp", invoker="bob")) is None
    monkeypatch.delenv("ECHO_MSAC_RULES")
    assert main.msac_rules(ALICE) is None


# Table: settings that cannot be read; each manages every caller with no rule (refused).
@pytest.mark.parametrize("value", [
    "not json",
    "[]",
    '{"alice": [{"dataNodeSelector": "/", "operations": ["read"]}]}',
    '{"alice": [{"dataNodeSelector": "/", "operations": "read", "action": "ALLOW"}]}',
    '{"alice": ["x"]}',
    '{"alice": [{"dataNodeSelector": "SubNetwork=A", "operations": ["read"], "action": "ALLOW"}]}',
    '{"alice": [{"dataNodeSelector": "/SubNetwork=A", "operations": ["read"], "action": "MAYBE"}]}',
    '{"alice": [{"dataNodeSelector": "/SubNetwork=A", "operations": ["write"], "action": "ALLOW"}]}',
    '{"alice": 5}',
])
def test_rules_that_cannot_be_read_fail_closed(monkeypatch, value):
    """A setting that is not JSON, not an object, or holds something that is not an AccessRule refuses every caller."""
    monkeypatch.setenv("ECHO_MSAC_RULES", value)
    assert main.msac_rules(ALICE) == []
    assert main.msac_rules(Caller(role="rapp", invoker="bob")) == []


@pytest.mark.parametrize(("item", "message"), [
    ({"dataNodeSelector": "/", "operations": ["read"]}, "an access rule is {dataNodeSelector, operations, action}"),
    ({"dataNodeSelector": "x", "operations": ["read"], "action": "ALLOW"}, "an access rule needs an absolute selector and ALLOW or DENY"),
    ({"dataNodeSelector": "/A=1", "operations": ["write"], "action": "DENY"}, "an access rule's operations are create, read, update, delete and exec"),
])
def test_rule_messages(item, message):
    """Each way a rule is wrong has its own message."""
    with pytest.raises(ValueError) as refused:
        main._rule(item)
    assert str(refused.value) == message


def test_a_rule_may_name_every_operation():
    """All five TS 28.319 operations at once are a valid rule."""
    item = {"dataNodeSelector": "/SubNetwork=A", "operations": list(msac.OPERATIONS), "action": "DENY"}
    assert main._rule(item) == msac.Rule("/SubNetwork=A", frozenset(msac.OPERATIONS), "DENY")
