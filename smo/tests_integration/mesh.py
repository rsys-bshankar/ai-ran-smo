"""In-process service mesh — makes cross-module httpx calls (R1Client) land on the right module's FastAPI TestClient
instead of going out over the network.

Deliberate scope choice: this bypasses R1 Termination's own proxy
mechanics (already covered by r1-termination/tests) and dispatches
directly, path-prefix-to-module, exactly as R1 Termination's ROUTES
table does. That keeps these tests focused on cross-module BUSINESS
LOGIC — does calling NFO actually make FOCOM's inventory get queried,
rather than re-proving the gateway's HTTP forwarding a second time.
"""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

# mirrors r1-termination/app/main.py's ROUTES table
R1_PREFIX_TO_SERVICE = {
    "/sme": "sme", "/dme": "dme",
    "/onboarding": "onboarding", "/rapp-mgmt": "rapp-mgmt", "/ran-nf-oam": "ran-nf-oam",
    "/nfo": "nfo", "/focom": "focom",
    # S2.1 / S2.2: the model registry and the deploy gate are AIMgF's, served at the root of its app; the old prefixes reach it until S2.4 deletes them
    "/aimgf": "aimgf", "/mlmr": "aimgf", "/mllf": "aimgf", "/ran-analytics": "ran-analytics", "/mdaf": "mdaf",
    "/intent-service": "intent-service", "/so-smos": "so-smos", "/sa-smos": "sa-smos",
}


class ServiceMesh:
    def __init__(self, clients: dict[str, "TestClient"]):
        """clients: {service-dir-name: TestClient}, e.g. {"sme": <TestClient>, "mock-o1-adaptor": <TestClient>}."""
        self.clients = clients

    def resolve(self, url: str) -> tuple["TestClient", str]:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path

        if host == "r1-termination":
            segments = path.lstrip("/").split("/", 1)
            prefix = "/" + segments[0]
            if prefix == "/rapps":
                return self._operator_api(segments[1] if len(segments) > 1 else "")
            service = R1_PREFIX_TO_SERVICE.get(prefix)
            if service is None:
                raise LookupError(f"no route for {prefix} in the mesh's R1 routing table")
            rest = "/" + segments[1] if len(segments) > 1 else "/"
            return self.clients[service], rest

        # direct calls that bypass R1 Termination entirely — e.g. RAN NF OAM's
        # O1 clients talking to the mock O1 adaptor (never routed through
        # R1 Termination at all).
        if host in self.clients:
            return self.clients[host], path

        raise LookupError(f"mesh has no route for host {host!r} ({url})")

    def _operator_api(self, rest: str) -> tuple["TestClient", str]:
        """The gateway's dynamic prefix `/rapps/{instanceId}/operator/<route>`: the base the instance registered at rApp Management (GUI-8.3) names the
        service, as the real gateway resolves it; an instance with none registered is a LookupError (the gateway answers 404)."""
        instance, _, tail = rest.partition("/")
        marker, _, route = tail.partition("/")
        if marker != "operator":
            raise LookupError(f"no route for /rapps/{rest} in the mesh")
        registered = self.clients["rapp-mgmt"].get(f"/instances/{instance}/operator-api")
        base = registered.json().get("operatorApiBase") if registered.status_code == 200 else None
        host = urlparse(base or "").hostname
        if host not in self.clients:
            raise LookupError(f"the instance {instance} has no operator API registered in the mesh")
        return self.clients[host], "/" + route

    def dispatch(self, verb: str, url: str, *, json=None, params=None, headers=None, content=None, timeout=None, **kwargs):
        client, rest_path = self.resolve(url)
        if urlparse(url).hostname == "r1-termination":
            # a call through R1 from R1Client is an SMO module's: R1 Termination would stamp it with the internal role (PR-SEC-14), which is what
            # lets a backend trust the X-R1-On-Behalf-Of header the module passed on
            headers = {**(headers or {}), "X-R1-Role": "internal"}
        method = getattr(client, verb)
        # httpx's own GET/DELETE signatures have no `json` parameter at all
        # (only POST/PUT/PATCH do) — passing it unconditionally raises
        # TypeError. Caught while running this harness for the first time:
        # every GET routed through the mesh (NFO's FOCOM inventory query) was hitting this before the fix.
        if verb in ("get", "delete"):
            return method(rest_path, params=params, headers=headers)
        # RAN NF OAM's netconf_client.py posts a raw XML body via `content=`,
        # not `json=` — every other caller until now was JSON-only, so this
        # branch never existed and `content` was silently dropped, always
        # forwarding an EMPTY body regardless of what the real caller sent.
        # Caught adding mock-o1-adaptor's own integration test (HISTORY.md
        # section 2): the request "succeeded" against an empty body and
        # looked like a genuine REJECTED outcome, not a harness bug.
        if content is not None:
            return method(rest_path, content=content, params=params, headers=headers)
        # Multipart uploads (MLMR's model artifact, `files=`) — dropped the
        # same way until the EnergySaving rApp (Wave 10.1) stored a model
        # through the mesh for the first time.
        if kwargs.get("files") is not None:
            return method(rest_path, files=kwargs["files"], params=params, headers=headers)
        return method(rest_path, json=json, params=params, headers=headers)


def install(monkeypatch, mesh: ServiceMesh) -> None:
    """Monkeypatches the plain httpx.get/post/put/patch/delete module
    functions — smo_shared.r1_client.R1Client calls these directly (`import httpx; httpx.post(...)`),
    so one patch at the httpx module level intercepts everything, regardless
    of which module's code makes the call. `patch` added for Wave 1's
    MLMR `PATCH /models/{id}/lifecycle` cross-service write-back — the
    first caller in this build that needed it; Wave 2 moved that same
    write-back to AIMgF's own `PATCH /models/{id}/runtime/node-groups`
    (MLLF's caller), still exercising this same verb.
    """
    import httpx

    from smo_shared import r1_client

    for verb in ("get", "post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, verb, (lambda v: lambda url, **kw: mesh.dispatch(v, url, **kw))(verb))
    # The mesh dispatches straight to each module, bypassing R1 Termination's
    # token gate along with the rest of its gateway mechanics (see the module
    # docstring) — so R1Client's SME token acquisition is skipped too. The
    # token flow itself is covered by shared/tests/test_r1_client.py.
    monkeypatch.setattr(r1_client, "_module_token", lambda base_url, refresh=False: None)
