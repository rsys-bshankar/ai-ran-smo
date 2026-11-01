"""The event stream (`GET /api/events`, app/events.py, GUI-9.1) and the typeahead (`GET /api/search`, app/search.py, GUI-9.2).

R1 Termination and SME are test_preferences_summary.py's CountingSmo (itself test_main.py's FakeSmo), extended here with the list answers the
typeahead reads (elements, rApp instances and packages, alarms, models, one decision record). Starlette's TestClient hands a response over only when
it is complete, so the stream tests shorten the stream's life, poll and ping to fractions of a second and read the whole stream at once.
Run: `PYTHONPATH=.:../shared python -m pytest tests/test_events_search.py -q`.
"""

import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import events, search, summary
from app.config import Settings
from app.db import Database
from app.main import create_app, seed_users
from app.smo_client import R1Gateway
from test_main import PASSWORDS, R1, login
from test_preferences_summary import CountingSmo

DECISION_ID = str(uuid.uuid4())
INSTANCE_ID = str(uuid.uuid4())
PACKAGE_ID = str(uuid.uuid4())
MODEL_ID = str(uuid.uuid4())


class SearchSmo(CountingSmo):
    """CountingSmo plus the answers the typeahead reads. `searches` records each typeahead call; a path in `failing` raises a connection error."""

    def __init__(self):
        super().__init__()
        self.searches: list[httpx.Request] = []
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        answers = {
            "/ran-nf-oam/managed-entities": {"items": [{"managedElementRef": "gnb-alpha-01", "vendorName": "acme", "region": "north"},
                                                      {"managedElementRef": "unrelated", "vendorName": "acme"}], "limit": 5, "offset": 0},
            "/ran-nf-oam/alarms": {"items": [{"alarmId": "a1", "managedElementRef": params.get("managed_element_ref"), "severity": "major",
                                              "ackState": "UNACKNOWLEDGED", "probableCause": "LINK_DOWN"}]},
            "/aimgf/models": {"items": [{"modelId": MODEL_ID, "modelType": "alpha-forecaster", "version": "1.0"},
                                       {"modelId": str(uuid.uuid4()), "modelType": "other", "version": "2.0"}]},
            "/rapp-mgmt/instances": {"items": [{"instanceId": INSTANCE_ID, "packageId": PACKAGE_ID, "state": "RUNNING"}], "hasMore": False},
            "/onboarding/packages": {"items": [{"packageId": PACKAGE_ID, "name": "alpha-saver", "version": "1.2.0", "vendor": "acme",
                                                "state": "PRIMED"}], "hasMore": False},
            f"/ran-nf-oam/decision-records/{DECISION_ID}": {"decisionId": DECISION_ID, "disposition": "DIRECT", "invokerId": "rapp-1"},
        }
        if request.url.host == "r1-termination" and request.method == "GET" and params.get("limit") != "1" and \
                (path in answers or path.startswith("/ran-nf-oam/decision-records/")):
            self.searches.append(request)
            if path in self.failing:
                raise httpx.ConnectError("down")
            if path not in answers:
                return httpx.Response(404, json={"title": "NOT_FOUND"})
            return httpx.Response(200, json=answers[path])
        return super().handler(request)


@pytest.fixture
def smo():
    """A fresh SearchSmo per test."""
    return SearchSmo()


@pytest.fixture
def app(smo):
    """The BFF on an in-memory database with the three seeded users, talking to `smo` through the real gateway client."""
    cfg = Settings(r1_url=R1, jwt_secret="test-secret", cookie_secure=False, admin_password=PASSWORDS["admin"],
                   operator_password=PASSWORDS["operator"], viewer_password=PASSWORDS["viewer"])
    db = Database("sqlite://")
    seed_users(db, cfg)
    return create_app(cfg, db=db, gateway=R1Gateway(R1, db, transport=httpx.MockTransport(smo.handler)))


@pytest.fixture
def quick(monkeypatch):
    """A stream that lives 0.6 s, polls every 0.05 s and pings every 0.2 s, with no summary cache, so a whole stream can be read in a test."""
    monkeypatch.setattr(events, "STREAM_MAX_SECONDS", 0.6)
    monkeypatch.setattr(events, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(events, "PING_SECONDS", 0.2)
    monkeypatch.setattr(summary, "CACHE_SECONDS", 0.0)


def parse(text: str) -> list[tuple[str, dict]]:
    """The (event, data) pairs of an SSE body; the `retry:` hint is skipped."""
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


# ------------------------------------------------------------------ the event stream

def test_a_stream_sends_the_full_counts_first_then_pings_and_nothing_while_nothing_changes(app, quick):
    """The first event carries every count of the page as changed; with steady counts no second summary follows, only pings."""
    resp = login(app, "viewer").get("/api/events", params={"topics": "summary:nav"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no" and resp.text.startswith("retry: ")
    got = parse(resp.text)
    summaries = [d for e, d in got if e == "summary"]
    assert len(summaries) == 1 and summaries[0]["page"] == "nav"
    assert summaries[0]["counts"]["alarms.critical"] == 1234 and set(summaries[0]["changed"]) == set(summary.PAGES["nav"])
    assert any(e == "ping" for e, _ in got)


def test_a_changed_count_is_pushed_with_only_the_keys_that_changed(app, smo, quick):
    """When one count moves, the next event names just that key in `changed`, and carries its new value."""
    original = smo.handler

    def growing(request):
        # every count call of critical alarms after the first answers one more
        if request.url.path == "/ran-nf-oam/alarms" and request.url.params.get("severity") == "critical":
            smo.totals[("/ran-nf-oam/alarms", "severity=critical")] += 1
        return original(request)

    smo.handler = growing
    app.state.gateway = R1Gateway(R1, app.state.db, transport=httpx.MockTransport(growing))
    summaries = [d for e, d in parse(login(app, "viewer").get("/api/events", params={"topics": "summary:nav"}).text) if e == "summary"]
    assert len(summaries) >= 2
    assert summaries[1]["changed"] == ["alarms.critical"] and summaries[1]["counts"]["alarms.critical"] > summaries[0]["counts"]["alarms.critical"]


def test_several_topics_each_get_their_page(app, quick):
    """Two topics on one stream: one first event per page, in the order asked."""
    got = parse(login(app, "viewer").get("/api/events", params={"topics": "summary:alarms, summary:rapps"}).text)
    assert [d["page"] for e, d in got if e == "summary"][:2] == ["alarms", "rapps"]


@pytest.mark.parametrize("topics,title", [("summary:nope", "UNKNOWN_TOPIC"), ("alarms", "UNKNOWN_TOPIC"), (" , ", "UNKNOWN_TOPIC"),
                                          ("summary:nav,summary:alarms,summary:rapps,summary:aiml,summary:software", "TOO_MANY_TOPICS")])
# An unknown page, a topic without the `summary:` kind, no topic at all, and five topics are each refused before any stream opens.
def test_bad_topics_are_a_400(app, topics, title):
    """Only `summary:<page>` topics of known pages, at most four, open a stream."""
    resp = login(app, "viewer").get("/api/events", params={"topics": topics})
    assert resp.status_code == 400 and resp.json()["title"] == title


def test_a_stream_needs_a_session(app):
    """Without a session cookie or token there is no stream."""
    assert TestClient(app).get("/api/events", params={"topics": "summary:nav"}).status_code == 401


def test_a_user_may_hold_at_most_five_streams(app):
    """The sixth stream of one user is a 429; another user is not affected by the first one's streams."""
    hub = app.state.event_hub
    for _ in range(events.MAX_STREAMS_PER_USER):
        hub.add(events._Subscriber("viewer", ("nav",), started=True))
    resp = login(app, "viewer").get("/api/events", params={"topics": "summary:nav"})
    assert resp.status_code == 429 and resp.json()["title"] == "TOO_MANY_STREAMS"
    assert hub.open_streams("operator") == 0


def test_a_closed_stream_is_forgotten_and_the_poller_stops(app, quick):
    """After a stream ends nothing of it stays in the hub, so its user's cap is free again and no poller runs for nobody."""
    login(app, "viewer").get("/api/events", params={"topics": "summary:nav"})
    hub = app.state.event_hub
    assert hub.subscribers == set() and (hub.task is None or hub.task.done())


def test_a_revoked_session_ends_the_stream_at_the_next_ping(app, smo, quick, monkeypatch):
    """The session is checked again at every ping: sessions revoked while the stream is open end it at the next ping, not at its 5 s deadline."""
    from app.db import GuiUser
    monkeypatch.setattr(events, "STREAM_MAX_SECONDS", 5.0)
    client = login(app, "viewer")
    original = smo.handler

    def revoking(request):
        # the first count call, made after the stream was accepted, is when an admin revokes the viewer's sessions
        if request.url.params.get("limit") == "1":
            with app.state.db.session() as s:
                s.get(GuiUser, "viewer").token_version += 1
                s.commit()
            smo.handler = original
        return original(request)

    smo.handler = revoking
    app.state.gateway = R1Gateway(R1, app.state.db, transport=httpx.MockTransport(lambda r: smo.handler(r)))
    started = time.monotonic()
    resp = client.get("/api/events", params={"topics": "summary:nav"})
    assert resp.status_code == 200 and time.monotonic() - started < 3
    assert not any(e == "ping" for e, _ in parse(resp.text))     # it ended at the check, before sending the ping



def test_an_event_on_the_smo_feed_pushes_the_change_before_the_next_poll(app, smo, monkeypatch):
    """GUI-1.5: the hub follows DME's event feed; an event there drops the summary cache and recomputes at once, so with a poll of a minute and a cache
    of a minute the raised alarm still reaches the stream within its one second. The first read only learns the newest position."""
    monkeypatch.setattr(events, "STREAM_MAX_SECONDS", 1.0)
    monkeypatch.setattr(events, "POLL_SECONDS", 60.0)
    monkeypatch.setattr(events, "PING_SECONDS", 0.3)
    monkeypatch.setattr(events, "KICK_SECONDS", 0.05)
    monkeypatch.setattr(events, "FEED_RETRY_SECONDS", 10.0)
    monkeypatch.setattr(summary, "CACHE_SECONDS", 60.0)
    original = smo.handler
    reads: list[dict] = []

    def with_feed(request):
        # the feed answers its position, then one alarm raised (and the count moves with it), then fails: the hub retries later, the stream goes on
        if request.url.path == events.FEED_PATH:
            reads.append(dict(request.url.params))
            if len(reads) == 1:
                return httpx.Response(200, json={"items": [], "next": 41})
            if len(reads) == 2:
                smo.totals[("/ran-nf-oam/alarms", "severity=critical")] = 1300
                return httpx.Response(200, json={"items": [{"type": "org.oran.smo.alarm.raised", "smoseq": 42}], "next": 42})
            raise httpx.ConnectError("dme down")
        return original(request)

    app.state.gateway = R1Gateway(R1, app.state.db, transport=httpx.MockTransport(with_feed))
    summaries = [d for e, d in parse(login(app, "viewer").get("/api/events", params={"topics": "summary:nav"}).text) if e == "summary"]
    assert [s["counts"]["alarms.critical"] for s in summaries] == [1234, 1300] and summaries[1]["changed"] == ["alarms.critical"]
    assert "after" not in reads[0] and reads[1]["after"] == "41" and float(reads[1]["wait"]) == events.FEED_WAIT_SECONDS
    hub = app.state.event_hub
    assert hub.subscribers == set() and (hub.follower is None or hub.follower.done())


def test_without_a_readable_feed_the_stream_polls_as_before(app, smo, quick):
    """An SMO whose DME has no event feed (404) changes nothing for a stream: the counts still come by the poll, and the hub says it is not following."""
    summaries = [d for e, d in parse(login(app, "viewer").get("/api/events", params={"topics": "summary:nav"}).text) if e == "summary"]
    assert summaries and summaries[0]["counts"]["alarms.critical"] == 1234 and app.state.event_hub.following is False

def test_a_subscriber_merges_changes_it_has_not_sent_yet():
    """Two changes before the browser reads: one event with the newest counts and the union of the changed keys, never a backlog."""
    sub = events._Subscriber("viewer", ("nav",))
    sub.offer({"page": "nav", "counts": {"a": 1, "b": 1}})
    sub.drain()
    sub.offer({"page": "nav", "counts": {"a": 2, "b": 1}})
    sub.offer({"page": "nav", "counts": {"a": 2, "b": 5}})
    sub.offer({"page": "nav", "counts": {"a": 2, "b": 5}})      # no change: nothing more
    assert sub.drain() == [{"page": "nav", "counts": {"a": 2, "b": 5}, "changed": ["a", "b"], "computedAt": None, "partial": []}]
    assert sub.drain() == []



def test_a_changed_panel_is_pushed_with_the_panels_and_the_counts_kept_apart():
    """A Dashboard panel that changes (a new decision) is pushed even when no count moved, as `panels` next to `counts`, named `panel.<name>`."""
    sub = events._Subscriber("viewer", ("dashboard",))
    sub.offer({"page": "dashboard", "counts": {"a": 1}, "panels": {"decisions": {"items": []}}})
    sub.drain()
    sub.offer({"page": "dashboard", "counts": {"a": 1}, "panels": {"decisions": {"items": [{"decisionId": "d1"}]}}})
    [event] = sub.drain()
    assert event["changed"] == ["panel.decisions"] and event["counts"] == {"a": 1}
    assert event["panels"] == {"decisions": {"items": [{"decisionId": "d1"}]}}

# ------------------------------------------------------------------ the typeahead

def test_search_groups_matches_by_kind_with_the_gui_route_of_each(app, smo):
    """Elements (by RAN NF OAM's `search`), rApps (the directory), alarms (by element ref), models (filtered here) each come back with their route."""
    body = login(app, "viewer").get("/api/search", params={"q": "alpha"}).json()
    groups = {g["type"]: g["items"] for g in body["groups"]}
    assert body["q"] == "alpha" and body["partial"] == []
    assert groups["element"] == [{"id": "gnb-alpha-01", "label": "gnb-alpha-01", "hint": "acme · north", "to": "/elements/gnb-alpha-01"}]
    assert groups["rapp"][0]["label"] == "alpha-saver" and groups["rapp"][0]["to"] == f"/rapps/{INSTANCE_ID}"
    assert groups["alarm"][0]["to"] == "/alarms?me=alpha"
    assert [m["id"] for m in groups["model"]] == [MODEL_ID] and groups["model"][0]["to"] == f"/aiml?model={MODEL_ID}#models"
    assert "decision" not in groups
    sent = {r.url.path: r for r in smo.searches}
    assert sent["/ran-nf-oam/managed-entities"].url.params["search"] == "alpha" and sent["/aimgf/models"].url.params["limit"] == "100"


def test_a_uuid_finds_its_decision(app):
    """A UUID is looked up as a decision record id, and opens `/decisions/<id>`."""
    body = login(app, "viewer").get("/api/search", params={"q": DECISION_ID}).json()
    decision = next(g for g in body["groups"] if g["type"] == "decision")
    assert decision["items"][0]["to"] == f"/decisions/{DECISION_ID}"


def test_text_with_spaces_does_not_ask_the_alarm_list(app, smo):
    """Alarms are asked by element ref, so a phrase that cannot be a ref costs no alarm call."""
    login(app, "viewer").get("/api/search", params={"q": "alpha saver"})
    assert not any(r.url.path == "/ran-nf-oam/alarms" for r in smo.searches)


def test_a_failing_source_is_partial_and_the_others_still_answer(app, smo):
    """A module that does not answer names its kind in `partial`; the other kinds still come back."""
    smo.failing.add("/aimgf/models")
    body = login(app, "viewer").get("/api/search", params={"q": "alpha"}).json()
    assert body["partial"] == ["model"] and {g["type"] for g in body["groups"]} >= {"element", "rapp"}


def test_a_slow_source_is_cut_off_at_its_timeout(app, smo, monkeypatch):
    """A source slower than its timeout is partial, and the answer does not wait for it."""
    import asyncio
    monkeypatch.setattr(search, "PER_SOURCE_TIMEOUT_SECONDS", 0.2)

    async def slow(text):
        await asyncio.sleep(5)
        return []

    app.state.rapp_search = slow
    started = time.monotonic()
    body = login(app, "viewer").get("/api/search", params={"q": "alpha"}).json()
    assert "rapp" in body["partial"] and time.monotonic() - started < 3


def test_a_query_under_two_characters_is_a_400(app):
    """One character would match nearly everything: refused before any module is asked."""
    resp = login(app, "viewer").get("/api/search", params={"q": " a "})
    assert resp.status_code == 400 and resp.json()["title"] == "QUERY_TOO_SHORT"


def test_search_answers_are_cached_for_a_short_time(app, smo):
    """The same question within the cache time costs no upstream call."""
    viewer = login(app, "viewer")
    viewer.get("/api/search", params={"q": "alpha"})
    calls = len(smo.searches)
    viewer.get("/api/search", params={"q": "ALPHA"})
    assert len(smo.searches) == calls


def test_search_needs_a_session(app):
    """Like every BFF route, the typeahead needs a signed-in user."""
    assert TestClient(app).get("/api/search", params={"q": "alpha"}).status_code == 401
