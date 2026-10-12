"""scripts/load_run.py: the pacing, the stop file and the timeline of errors that the upgrade-under-load lane (PR-V-10) reads."""

import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("load_run", SMO_ROOT / "scripts" / "load_run.py")
load_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_run)


def run_worker(monkeypatch, statuses, seconds=0.3, interval=0.0, stop_file=None):
    """Runs one load worker for `seconds` with `one_call` replaced by a script that returns the given statuses in order (200 after they run out)
    and returns (per-route series, error timeline).
    """
    calls = iter(statuses)

    async def one_call(client, gateway, sme, reg, access, route):
        try:
            return next(calls)
        except StopIteration:
            return 200

    monkeypatch.setattr(load_run, "one_call", one_call)
    series = {route[0]: load_run.Series() for route in load_run.ROUTES}
    timeline: dict[int, list[int]] = {}
    import random
    now = time.monotonic()
    asyncio.run(load_run.worker(None, "g", "s", {}, "t", now + seconds, now, series, [r[3] for r in load_run.ROUTES], random.Random(1), timeline, interval, stop_file))
    return series, timeline


def test_a_call_is_an_error_when_its_status_is_not_the_one_the_route_expects_and_the_timeline_counts_both(monkeypatch):
    """A call whose status is not the one the route expects counts as an error, and the timeline's call and error totals equal what the per-route
    series recorded.
    """
    series, timeline = run_worker(monkeypatch, [200, 503, 0, 200])
    assert sum(s.errors for s in series.values()) >= 2
    calls = sum(cell[0] for cell in timeline.values())
    assert calls == sum(len(s.latencies_ms) for s in series.values()) and sum(cell[1] for cell in timeline.values()) == sum(s.errors for s in series.values())


def test_the_pacing_keeps_each_call_at_least_one_interval_apart(monkeypatch):
    """With an interval the worker makes about five calls in half a second, not the thousands an unpaced loop makes."""
    series, _ = run_worker(monkeypatch, [], seconds=0.5, interval=0.1)
    assert 3 <= sum(len(s.latencies_ms) for s in series.values()) <= 6          # about 5 in half a second, not the thousands an unpaced loop makes


def test_a_stop_file_ends_the_worker_before_its_deadline(monkeypatch, tmp_path):
    """A worker whose stop file exists ends at once without a call, long before its 30 second deadline, and `stopped` is true only for an existing
    file.
    """
    stop = tmp_path / "stop"
    stop.write_text("")
    started = time.monotonic()
    series, _ = run_worker(monkeypatch, [], seconds=30, stop_file=str(stop))
    assert time.monotonic() - started < 2 and sum(len(s.latencies_ms) for s in series.values()) == 0
    assert load_run.stopped(str(stop)) and not load_run.stopped(str(tmp_path / "no")) and not load_run.stopped(None)


def test_the_timeline_rows_name_each_ten_second_slice():
    """Timeline rows give each ten-second slice's start and end, calls and errors, in order and without the empty slices."""
    rows = load_run.timeline_rows({2: [50, 3], 0: [100, 0]})
    assert rows == [{"from_s": 0, "to_s": 10, "calls": 100, "errors": 0}, {"from_s": 20, "to_s": 30, "calls": 50, "errors": 3}]


def test_the_markdown_says_when_the_errors_were_or_that_there_were_none():
    """The report names the slices that had errors, the run length, the warm-up and the pacing, and says there were no errors when there were none."""
    base = {"routes": [], "total": {"route": "all", "requests": 150, "rps": 15.0, "p50": 1, "p95": 2, "p99": 3, "max": 4, "errors": 3}, "seconds": 30.0}
    args = SimpleNamespace(concurrency=4, duration=60, warmup=5, rate=20)
    bad = load_run.markdown({**base, "timeline": load_run.timeline_rows({2: [50, 3]})}, args)
    assert "Errors by time since the end of the warm-up: 20-30 s: 3 of 50" in bad and "for 30 s after a 5 s warm-up, paced to about 20 calls a second" in bad
    assert "No errors in any 10 s of the run." in load_run.markdown({**base, "timeline": load_run.timeline_rows({0: [10, 0]})}, SimpleNamespace(concurrency=4, duration=60, warmup=5, rate=0))


def test_a_failed_call_is_listed_with_its_status_and_the_start_of_the_answer(monkeypatch):
    """Each failed call is listed with its route, status and the start of the answer, up to the maximum."""
    class Refused(int):
        detail = '{"code":"AUTH_SERVICE_UNAVAILABLE"}'

    async def one_call(client, gateway, sme, reg, access, route):
        return Refused(503)

    monkeypatch.setattr(load_run, "one_call", one_call)
    series = {route[0]: load_run.Series() for route in load_run.ROUTES}
    failures: list[dict] = []
    import random
    now = time.monotonic()
    asyncio.run(load_run.worker(None, "g", "s", {}, "t", now + 0.05, now, series, [r[3] for r in load_run.ROUTES], random.Random(1), {}, 0.0, None, failures))
    assert failures and failures[0]["status"] == 503 and "AUTH_SERVICE_UNAVAILABLE" in failures[0]["detail"] and failures[0]["route"] in series
    assert len(failures) == sum(s.errors for s in series.values()) or len(failures) == load_run.MAX_FAILURES


def test_a_call_sends_the_token_a_refresh_put_in_the_holder_and_still_takes_a_plain_string():
    """`one_call` reads the token from a `Bearer` at the moment of the call (so a refresh reaches a running worker) and accepts a plain string too."""
    seen = []

    class Client:
        async def get(self, url, headers):
            seen.append(headers["Authorization"])
            return SimpleNamespace(status_code=200, text="")

    route = next(r for r in load_run.ROUTES if r[1] == "GET")
    holder = load_run.Bearer("first")
    asyncio.run(load_run.one_call(Client(), "g", "s", {"apiInvokerId": "i"}, holder, route))
    holder.value = "second"
    asyncio.run(load_run.one_call(Client(), "g", "s", {"apiInvokerId": "i"}, holder, route))
    asyncio.run(load_run.one_call(Client(), "g", "s", {"apiInvokerId": "i"}, "plain", route))
    assert seen == ["Bearer first", "Bearer second", "Bearer plain"]


def run_main(monkeypatch, tmp_path, token_refresh, seconds=0.5):
    """Runs `main_async` for `seconds` against a fake SME (each token request answers `token-<n>`) with a worker that only sleeps and reports the token it holds at the end;
    returns (that last token, the number of tokens taken)."""
    taken = []

    async def register(client, sme):
        return {"apiInvokerId": "i", "onboardingSecret": "s"}

    async def get_token(client, sme, reg):
        taken.append(len(taken) + 1)
        n = taken[-1]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"access_token": f"token-{n}"})

    last = []

    async def worker(client, gateway, sme, reg, access, deadline, *rest):
        await asyncio.sleep(seconds)
        last.append(access.value)

    async def scrape(client, *urls):
        return None

    monkeypatch.setattr(load_run, "register", register)
    monkeypatch.setattr(load_run, "get_token", get_token)
    monkeypatch.setattr(load_run, "worker", worker)
    monkeypatch.setattr(load_run, "scrape", scrape)
    args = SimpleNamespace(concurrency=1, rate=0.0, warmup=0.0, duration=seconds, seed=1, token_refresh=token_refresh, gateway="g", sme="s", sme_metrics="m", gateway_metrics="m",
                           out=str(tmp_path), stop_file=None, timeout=5.0, max_error_rate=0.0)
    asyncio.run(load_run.main_async(args))
    return last[0], len(taken)


def test_a_long_run_takes_a_new_token_before_the_first_expires(monkeypatch, tmp_path):
    """With `--token-refresh` the run takes new tokens while it goes (the soak runs two hours on tokens that last one) and the workers end holding the latest; with 0 it takes only the first."""
    held, taken = run_main(monkeypatch, tmp_path, token_refresh=0.1)
    assert taken >= 3 and held == f"token-{taken}"
    held, taken = run_main(monkeypatch, tmp_path, token_refresh=0)
    assert (held, taken) == ("token-1", 1)
