"""The stub's periodic heartbeat: what a real gNB's O1 agent does without being asked, so its endpoint at RAN NF OAM stays ACTIVE.

RAN NF OAM ages an endpoint whose last heartbeat is older than 90 s (`MISSED_HEARTBEAT_THRESHOLD`) out of ACTIVE at the moment it is used, and refuses an
O1 write to it. The stub sends heartbeats only on request (`POST /emit/heartbeat`), so in a demo the endpoint went stale 90 s after step 00's one press of
Heartbeat, and a config change made later failed. With `MOCK_O1_HEARTBEAT_SECONDS` above 0 (default 0, off) a daemon thread sends one, every that many
seconds, for each endpoint registered at RAN NF OAM (`MOCK_O1_OAM_URL`) whose adaptor URI is this stub's (it starts with `MOCK_O1_SELF_URL`) and that has
had its first heartbeat already: the first one is still the operator's (it moves the endpoint from DISCOVERED and applies an onboarding template), and an
endpoint of another adaptor is never touched. Like the `/emit/*` routes it calls RAN NF OAM directly over plain HTTP, on the compose network.
"""

import logging
import os
import threading
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)
TIMEOUT_SECONDS = 5.0
PAGE = 500


def interval() -> float:
    """`MOCK_O1_HEARTBEAT_SECONDS` as a number of seconds; 0 (off) when unset, not a number, or not above 0."""
    try:
        return max(0.0, float(os.environ.get("MOCK_O1_HEARTBEAT_SECONDS", "0")))
    except ValueError:
        return 0.0


def beat_once(oam_url: str, self_url: str) -> int:
    """Sends one heartbeat for every endpoint of this stub that has had its first one (see the module docstring); returns how many RAN NF OAM accepted.
    A failure (RAN NF OAM down, an endpoint deleted meanwhile) is logged and the next tick tries again."""
    oam_url, self_url = oam_url.rstrip("/"), self_url.rstrip("/")
    sent, offset = 0, 0
    while True:
        page = httpx.get(f"{oam_url}/o1-adaptor-endpoints", params={"limit": PAGE, "offset": offset}, timeout=TIMEOUT_SECONDS).json()
        for ep in page.get("items", []):
            uri = ep.get("adaptorUri") or ""
            if not ep.get("lastHeartbeatAt") or not (uri == self_url or uri.startswith(self_url + "/")):
                continue
            resp = httpx.post(f"{oam_url}/o1-adaptor-endpoints/{quote(ep['endpointId'], safe='')}/heartbeat", timeout=TIMEOUT_SECONDS)
            if 200 <= resp.status_code < 300:
                sent += 1
            else:
                log.warning("heartbeat of endpoint %s answered %s", ep["endpointId"], resp.status_code)
        offset += PAGE
        if offset >= page.get("total", 0):
            return sent


def start() -> threading.Event | None:
    """Starts the heartbeat thread when `MOCK_O1_HEARTBEAT_SECONDS`, `MOCK_O1_OAM_URL` and `MOCK_O1_SELF_URL` are all set; returns the event that stops it,
    or None when it is off."""
    every, oam_url, self_url = interval(), os.environ.get("MOCK_O1_OAM_URL", "").strip(), os.environ.get("MOCK_O1_SELF_URL", "").strip()
    if not (every and oam_url and self_url):
        return None
    stop = threading.Event()

    def run() -> None:
        """The loop: one round every `every` seconds until `stop` is set; a failed round is logged, never fatal."""
        while not stop.wait(every):
            try:
                beat_once(oam_url, self_url)
            except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
                log.warning("heartbeat round to %s failed: %s", oam_url, type(exc).__name__)

    threading.Thread(target=run, name="o1-heartbeat", daemon=True).start()
    log.info("sending a heartbeat every %g s for this adaptor's endpoints at %s", every, oam_url)
    return stop
