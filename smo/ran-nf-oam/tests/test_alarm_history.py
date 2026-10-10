"""MGT-8.2 and MGT-8.3: the history of every alarm and the comments on it.

Covered: every change of an alarm becomes a history row in the same transaction, whatever path made it (the ack and clear routes, an ORM change
made anywhere else, a raise); the history route answers it oldest first with who did what; comments are added, listed oldest first, trimmed and
refused when blank, too long or carrying an unknown field; an unknown alarm, or one outside the caller's scope claim, is a 404 for all three
routes. Fixtures `client` and `db_session_factory` from `test_main.py` (a fresh in-memory SQLite per test); `alarm` below registers one element
and writes one alarm through the ORM. Run with: `cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/test_alarm_history.py -q`.
"""

import json
import uuid

import pytest
from smo_shared.scope import SCOPE_HEADER

from app.models import Alarm, AlarmHistory, ManagedEntity

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)

OUTSIDE = {"X-R1-Invoker-Id": "op", "X-R1-Role": "internal", SCOPE_HEADER: json.dumps({"regions": ["us"]})}


@pytest.fixture
def alarm(db_session_factory):
    """One major alarm on ME-1 (region eu), raised through the ORM; returns its id."""
    with db_session_factory() as db:
        db.add(ManagedEntity(managed_element_ref="ME-1", entity_type="O-DU", o1_protocol="NETCONF", region="eu", managed_function_ref="GNBDU-me-1"))
        a = Alarm(source_alarm_id="s-1", managed_element_ref="ME-1", severity="major")
        db.add(a)
        db.commit()
        return str(a.alarm_id)


def _history(client, alarm_id, **headers):
    """The history route's events as (event, from, to, by) tuples, oldest first."""
    body = client.get(f"/alarms/{alarm_id}/history", headers=headers).json()
    return [(h["event"], h["from"], h["to"], h["by"]) for h in body["items"]]


def test_the_ack_unack_and_clear_routes_are_recorded_with_who_did_them(client, alarm):
    """Raise, acknowledge, unacknowledge and clear each leave one row, oldest first, with the user the route was given."""
    assert client.patch(f"/alarms/{alarm}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "smo-gui:ana"}).status_code == 200
    assert client.patch(f"/alarms/{alarm}/ack", params={"new_state": "UNACKNOWLEDGED", "ack_user_id": "smo-gui:bob"}).status_code == 200
    assert client.patch(f"/alarms/{alarm}/clear", params={"clear_user_id": "smo-gui:ana"}).status_code == 200
    assert _history(client, alarm) == [("RAISED", None, "major", None), ("ACKNOWLEDGED", "UNACKNOWLEDGED", "ACKNOWLEDGED", "smo-gui:ana"),
                                       ("UNACKNOWLEDGED", "ACKNOWLEDGED", "UNACKNOWLEDGED", "smo-gui:bob"), ("CLEARED", "major", "cleared", "smo-gui:ana")]
    assert client.get(f"/alarms/{alarm}/history").json()["total"] == 4


def test_a_change_made_anywhere_through_the_orm_is_recorded(client, alarm, db_session_factory):
    """A re-grade written by some other path (the VES receiver does this) is recorded too: the listener, not the route, writes the history; a
    repeated ack that changes nothing adds no row."""
    with db_session_factory() as db:
        db.get(Alarm, uuid.UUID(alarm)).severity = "critical"
        db.commit()
    client.patch(f"/alarms/{alarm}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "u"})
    client.patch(f"/alarms/{alarm}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "u"})
    assert [h[0] for h in _history(client, alarm)] == ["RAISED", "SEVERITY_CHANGED", "ACKNOWLEDGED"]
    with db_session_factory() as db:
        assert db.query(AlarmHistory).filter(AlarmHistory.event == "SEVERITY_CHANGED").one().to_value == "critical"


def test_comments_are_added_and_listed_oldest_first(client, alarm):
    """A comment is 201 with its id and time; the list holds them in the order written, the text trimmed."""
    first = client.post(f"/alarms/{alarm}/comments", json={"author": "smo-gui:ana", "text": "  fibre cut reported by field team  "})
    assert first.status_code == 201 and first.json()["text"] == "fibre cut reported by field team" and first.json()["author"] == "smo-gui:ana"
    client.post(f"/alarms/{alarm}/comments", json={"author": "smo-gui:bob", "text": "crew on site"})
    body = client.get(f"/alarms/{alarm}/comments").json()
    assert [c["text"] for c in body["items"]] == ["fibre cut reported by field team", "crew on site"] and body["total"] == 2


@pytest.mark.parametrize("payload", [{"author": "a", "text": "   "}, {"author": "a", "text": "x" * 2001}, {"author": "", "text": "x"},
                                     {"author": "a", "text": "x", "pinned": True}, {"text": "x"}])
# A blank text, one over 2000 characters, an empty author, an unknown field and a missing author are each refused.
def test_a_malformed_comment_is_a_422(client, alarm, payload):
    """Nothing is stored for a comment the route refuses."""
    assert client.post(f"/alarms/{alarm}/comments", json=payload).status_code == 422
    assert client.get(f"/alarms/{alarm}/comments").json()["total"] == 0


@pytest.mark.parametrize("route", ["history", "comments"])
# The two reads and the write answer 404 for an alarm that does not exist and for one the caller's scope claim does not reach.
def test_an_unknown_or_out_of_scope_alarm_is_a_404(client, alarm, route):
    """No route tells a caller that an alarm it may not see exists."""
    assert client.get(f"/alarms/{uuid.uuid4()}/{route}").status_code == 404
    assert client.get(f"/alarms/{alarm}/{route}", headers=OUTSIDE).status_code == 404
    assert client.post(f"/alarms/{alarm}/comments", json={"author": "a", "text": "x"}, headers=OUTSIDE).status_code == 404
