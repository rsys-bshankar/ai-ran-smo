"""The console's event stream (GUI-9.1): `GET /api/events?topics=summary:nav,summary:alarms`, Server-Sent Events.

A page that shows counts used to poll `GET /api/summary/{page}` every few seconds, once per open tab. Here one background poller per BFF process
recomputes each page that at least one stream subscribed to, every `POLL_SECONDS`, through app/summary.py's own cached computation
(`app.state.summary_page`, so a page asked by tiles and pushed to streams is still one fan-out per cache period), and sends a stream an
`event: summary` only when a count it holds has changed. Every `PING_SECONDS` a stream gets an `event: ping`, which keeps every proxy on the way
(nginx reads with a 60 s timeout by default) from closing an idle connection, and at that moment the session is checked again, so a revoked
session or a deactivated user loses the stream within one ping.

Installed by app/main.py. Reads only. What a maintainer must know:

- A topic is `summary:<page>` with a page of `summary.PAGES`, optionally narrowed to a scope (GUI-9.3): `summary:<page>@<region>` or
  `summary:<page>@<region>/<site_cluster>`, each name checked against `summary.SCOPE_RE`; or `summary:attention` (GUI-9.8b, the Dashboard's
  "Needs your attention" groups, also with an optional `@<region>[/<site_cluster>]`), sent as `event: attention`. Nothing else is a topic yet
  (alarms, approvals and decisions as raw events would need the modules to publish them; the counts are what the console patches today).
  Within the hub a topic is keyed by what follows `summary:` (`nav`, `nav@north/c1`, `attention`). At most `MAX_TOPICS` per stream, at most
  `MAX_STREAMS_PER_USER` open streams per user in this process (several BFF instances each count their own).
- GUI-5: a topic is computed for the subscriber's scope claim (app/scoping.py) and keyed in the hub by it too (`nav|{"regions":["eu"]}`), so streams of users
  with different claims never share counts; the claim never reaches the browser (its events name the topic it asked for). A stream whose user's claim changed
  ends at the next ping, and the browser reconnects with the new one.
- A stream ends by itself after `STREAM_MAX_SECONDS`; the browser's EventSource reconnects after `RETRY_MS`, which also re-reads the session.
  The tests shorten the three timings to read a whole stream with the buffering TestClient.
- What each stream has been sent is kept per stream (`_Subscriber.seen`), so "changed" is relative to what that browser holds, and a stream that
  opens later than another gets the full counts first, then only changes.
- The response carries `X-Accel-Buffering: no`, and smo/gui/nginx.conf has its own location for `/api/events` with buffering off: a buffering
  proxy would hold every event until its buffer fills.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import summary

log = logging.getLogger("smo-gui-bff")

POLL_SECONDS = 5.0
PING_SECONDS = 15.0
STREAM_MAX_SECONDS = 1800.0
START_GRACE_SECONDS = 30.0      # a stream reserved by the route but never started by the server (the client left at once) is dropped after this
RETRY_MS = 3000
MAX_TOPICS = 4
MAX_STREAMS_PER_USER = 5
TOPIC_PREFIX = "summary:"
CLAIM_SEP = "|"                 # GUI-5: separates a topic's key from the subscriber's scope claim in the hub's key (no topic or claim value holds it)
ATTENTION = "attention"


@dataclass(frozen=True)
class Topic:
    """One parsed topic: its key (what follows `summary:`), the page (a key of `summary.PAGES`, or "attention"), the place (None: none) and, GUI-5, the
    subscriber's stored scope claim it is computed for (None: unscoped)."""
    key: str
    page: str
    region: str | None = None
    site_cluster: str | None = None
    claim: str | None = None

    @property
    def hub_key(self) -> str:
        """The key the hub computes and caches this topic under: the topic's key, and the claim when there is one."""
        return self.key if self.claim is None else f"{self.key}{CLAIM_SEP}{self.claim}"


def parse_topic(raw: str) -> Topic | None:
    """`summary:<page>[@<region>[/<site_cluster>]]` as a `Topic`, or None when it is not one: not the `summary:` kind, an unknown page, or a
    scope name `summary.SCOPE_RE` refuses (it would otherwise reach a module and the cache key unchecked)."""
    if not raw.startswith(TOPIC_PREFIX):
        return None
    key = raw[len(TOPIC_PREFIX):]
    page, at, scope = key.partition("@")
    if page != ATTENTION and page not in summary.PAGES:
        return None
    if not at:
        return Topic(key, page)
    region, slash, cluster = scope.partition("/")
    if not summary.SCOPE_RE.match(region) or (slash and not summary.SCOPE_RE.match(cluster)):
        return None
    return Topic(key, page, region, cluster if slash else None)


def sse(event: str, data: dict) -> str:
    """One Server-Sent Event: the `event:` line, one `data:` line of compact JSON (it never holds a newline) and the blank line that ends it."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@dataclass(eq=False)
class _Subscriber:
    """One open stream: who, which topics (by key: `nav`, `nav@north`, `attention`), what it was last sent per topic (`seen`) and what is waiting
    to be sent (`pending`)."""
    username: str
    pages: tuple[str, ...]
    created: float = field(default_factory=time.monotonic)
    started: bool = False
    seen: dict[str, dict] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, body: dict, key: str | None = None) -> None:
        """Queue `body` for this stream's topic `key` (default `body["page"]`) if anything differs from what it was last offered: a count, or
        for the attention topic a group's total or rows (`changed` then names the group types). A change not yet sent is merged with the new one
        (newest values, union of the changed keys), so a slow browser gets one event per topic, never a backlog. A scoped topic's event also
        carries `topic` and `scope`."""
        page = body["page"]
        key = key or page
        shown = key.split(CLAIM_SEP, 1)[0]                  # the topic as the browser asked for it: the claim is the hub's, not the browser's
        if page == ATTENTION:
            values = {g["type"]: {"total": g.get("total"), "items": g.get("items")} for g in body.get("groups") or []}
        else:
            # a page's panels (GUI-9.11) are compared with its counts, so a new decision or a changed health group is pushed too
            values = {**(body.get("counts") or {}), **{f"panel.{k}": v for k, v in (body.get("panels") or {}).items()}}
        before = self.seen.get(key)
        changed = [k for k, v in values.items() if before is None or k not in before or before[k] != v]
        if not changed:
            return
        self.seen[key] = dict(values)
        earlier = self.pending.get(key)
        if earlier is not None:
            changed = sorted(set(changed) | set(earlier["changed"]))
        event: dict = {"page": page}
        if page == ATTENTION:
            event["groups"] = list(body.get("groups") or [])
        else:
            event["counts"] = dict(body.get("counts") or {})
            if "panels" in body:
                event["panels"] = dict(body["panels"])
        event.update({"changed": changed, "computedAt": body.get("computedAt"), "partial": body.get("partial", [])})
        if body.get("scope") is not None or shown != page:
            event.update({"topic": TOPIC_PREFIX + shown, "scope": body.get("scope"), "unscoped": body.get("unscoped", [])})
        self.pending[key] = event
        self.wake.set()

    def drain(self) -> list[dict]:
        """The events waiting for this stream, in topic order, and an empty queue."""
        out = [self.pending.pop(p) for p in self.pages if p in self.pending]
        self.wake.clear()
        return out


class EventHub:
    """The streams of this process and the one poller that feeds them. The poller runs only while a stream is open: the first stream starts it,
    the last one to close cancels it."""

    def __init__(self, page_counts: Callable[[Topic], Awaitable[dict]]):
        self.page_counts = page_counts
        self.subscribers: set[_Subscriber] = set()
        self.task: asyncio.Task | None = None

    def open_streams(self, username: str) -> int:
        return sum(1 for s in self.subscribers if s.username == username)

    def add(self, sub: _Subscriber) -> None:
        self.subscribers.add(sub)

    def remove(self, sub: _Subscriber) -> None:
        """Forget `sub` (idempotent); with no stream left, stop the poller unless it is the caller."""
        self.subscribers.discard(sub)
        # The poller itself removing the last stream (a stale one) just lets its loop end; anyone else cancels it.
        if not self.subscribers and self.task is not None and not self.task.done() and asyncio.current_task() is not self.task:
            self.task.cancel()
            self.task = None

    def ensure_poller(self) -> None:
        """Start the poller on the running loop unless one is already running there (a test client may run each request on its own loop)."""
        loop = asyncio.get_running_loop()
        if self.task is None or self.task.done() or self.task.get_loop() is not loop:
            self.task = loop.create_task(self._run())

    async def _page(self, key: str) -> dict | None:
        """The summary body of the topic `key` (app/summary.py's cached computation), or None when the key does not parse or computing it raised.
        Never raises."""
        base, _, claim = key.partition(CLAIM_SEP)
        topic = parse_topic(TOPIC_PREFIX + base)
        if topic is None:
            return None
        topic = replace(topic, claim=claim or None)
        try:
            return await self.page_counts(topic)
        except Exception:        # deliberate catch-all: one bad page must not stop the poller of every other stream
            log.exception("event stream: summary topic %s failed", key)
            return None

    async def _run(self) -> None:
        """Every POLL_SECONDS: drop streams that never started, recompute each subscribed page once and offer it to its streams."""
        while self.subscribers:
            now = time.monotonic()
            for stale in [s for s in self.subscribers if not s.started and now - s.created > START_GRACE_SECONDS]:
                self.remove(stale)
            live = [s for s in self.subscribers if s.started]
            keys = sorted({p for s in live for p in s.pages})
            bodies = await asyncio.gather(*(self._page(k) for k in keys))
            for key, body in zip(keys, bodies):
                if body is None:
                    continue
                for s in live:
                    if key in s.pages:
                        s.offer(body, key)
            await asyncio.sleep(POLL_SECONDS)


def install(app: FastAPI, *, current_session: Callable, problem: Callable[..., JSONResponse]) -> None:
    """Add `GET /api/events` to `app`. Needs app/summary.py installed first (it reads `app.state.summary_page` when a stream opens)."""
    async def fetch(topic: Topic) -> dict:
        # the attention groups or a page's counts, from app/summary.py's cached computations (shared with the HTTP routes)
        if topic.page == ATTENTION:
            return await app.state.summary_attention(topic.region, topic.site_cluster, claim=topic.claim)
        return await app.state.summary_page(topic.page, topic.region, topic.site_cluster, topic.claim)

    hub = EventHub(fetch)
    app.state.event_hub = hub

    def session_still_valid(request: Request, claim: str | None) -> bool:
        """The same checks as when the stream opened (signature, user active, token version, logout), and the user's scope claim still `claim` (GUI-5): False
        once any fails."""
        try:
            return current_session(request).user.scope == claim
        except Exception:        # deliberate: whatever the check raises (its HTTP 401 / 403), the stream must end, not fail
            return False

    @app.get("/api/events", responses={200: {"description": "A `text/event-stream`: `event: summary` with "
                                                            "`{page, counts, changed, computedAt, partial}` when a count changed (the full counts first; a scoped topic adds "
                                                            "`topic`, `scope`, `unscoped`), `event: attention` with `{page, groups, changed, ...}` for "
                                                            "`summary:attention`, `event: ping` every 15 s", "content": {"text/event-stream": {}}}})
    async def events(request: Request, topics: str = Query(..., max_length=400, description="Comma-separated, at most 4: `summary:<page>` for a page "
                                                                                            "of `GET /api/summary/{page}`, or `summary:attention`; "
                                                                                            "either may end in `@<region>` or `@<region>/<site_cluster>`."),
                     session=Depends(current_session)):
        """Server-Sent Events of the summary counts of up to four console pages, each optionally scoped, or of the attention groups. 400
        `UNKNOWN_TOPIC` (also a malformed scope) / `TOO_MANY_TOPICS`, 403 when a count of a page needs a read the role does not have, 429
        `TOO_MANY_STREAMS` past five open streams of one user."""
        wanted: list[str] = []
        for raw in topics.split(","):
            topic = raw.strip()
            if topic and topic not in wanted:
                wanted.append(topic)
        if not wanted:
            return problem(400, "UNKNOWN_TOPIC", "name at least one topic: summary:<page>")
        if len(wanted) > MAX_TOPICS:
            return problem(400, "TOO_MANY_TOPICS", f"at most {MAX_TOPICS} topics per stream")
        parsed = [parse_topic(t) for t in wanted]
        unknown = [t for t, p in zip(wanted, parsed) if p is None]
        if unknown:
            return problem(400, "UNKNOWN_TOPIC", f"unknown topic {unknown[0]!r}: topics are summary:<page>[@<region>[/<site_cluster>]] with a page of "
                                                 f"{', '.join(sorted([*summary.PAGES, ATTENTION]))}")
        topics_ = [replace(p, claim=session.user.scope) for p in parsed if p is not None]
        pages = tuple(p.hub_key for p in topics_)
        if not all(summary.page_allowed(p.page, session.user.role) for p in topics_):
            return problem(403, "FORBIDDEN", "a count on one of these pages needs a read your role does not have")
        if hub.open_streams(session.user.username) >= MAX_STREAMS_PER_USER:
            return problem(429, "TOO_MANY_STREAMS", f"at most {MAX_STREAMS_PER_USER} open event streams per user: close another tab")
        # Reserved here, so the cap holds against streams opened at the same moment; the generator below always removes it, and the poller drops
        # one whose response never started (START_GRACE_SECONDS).
        sub = _Subscriber(session.user.username, pages)
        hub.add(sub)

        async def stream():
            """The stream body: the retry hint, the full counts of each page, then changes and pings until the deadline, a failed session check or
            the browser leaving (Starlette cancels the generator, and `finally` removes the stream)."""
            try:
                sub.started = True
                yield f"retry: {RETRY_MS}\n\n"
                for key, body in zip(pages, await asyncio.gather(*(hub._page(p) for p in pages))):
                    if body is not None:
                        sub.offer(body, key)
                hub.ensure_poller()
                now = time.monotonic()
                deadline, next_ping = now + STREAM_MAX_SECONDS, now + PING_SECONDS
                while True:
                    for event in sub.drain():
                        yield sse(ATTENTION if event["page"] == ATTENTION else "summary", event)
                    now = time.monotonic()
                    if now >= deadline:
                        return
                    if now >= next_ping:
                        if not await run_in_threadpool(session_still_valid, request, session.user.scope):
                            return
                        yield sse("ping", {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                        next_ping = now + PING_SECONDS
                    try:
                        await asyncio.wait_for(sub.wake.wait(), timeout=max(0.0, min(next_ping, deadline) - now))
                    except TimeoutError:
                        pass
            finally:
                hub.remove(sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
