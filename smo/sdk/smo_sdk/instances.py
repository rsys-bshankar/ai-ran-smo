"""An rApp instance's own identity, inside the rApp process (docs/call-flows/01-rapp-onboarding-to-deployment.md, 01c and 01d).

rApp Management makes an identity at SME for every instance it creates (with the instance's scope claim; the instance's limits, kill switch and
approval policy key on it) and pushes the credentials to the instance's operator API (`POST /instances/{id}/credentials`, through R1 Termination).
One rApp process may serve several instances, so the SDK keeps one identity per instance (`smo_shared.r1_client.bind_instance_identity`, in memory
only) and every call made while handling a request for `/instances/{id}/...` carries that instance's token. A call for an instance with no
credentials, or outside any instance, carries the process's own.

    from smo_sdk import instances
    instances.install(app)                      # the credentials route, and acting for the instance of each /instances/{id}/... request
    sdk.platform.bootstrap_complete(instance_id)   # DEPLOYING -> RUNNING, as the instance itself

The credentials are kept in memory: after a restart the rApp acts for none of its instances until rApp Management issues them again
(`POST /rapp-mgmt/instances/{id}/credentials`).
"""

import re

from smo_shared.r1_client import bind_instance_identity, instance_identity, reset_acting_instance, set_acting_instance
from smo_shared.roles import ROLE_HEADER, ROLE_INTERNAL

__all__ = ["install", "is_bound", "invoker_of", "bootstrap"]

_INSTANCE_PATH = re.compile(r"^/instances/([0-9a-fA-F-]{36})(/|$)")


def is_bound(instance_id: str) -> bool:
    """Whether this process holds the credentials of the instance."""
    return instance_identity(str(instance_id)) is not None


def invoker_of(instance_id: str) -> str | None:
    """The instance's invoker id (its `oauthClientId`), when its credentials are held."""
    return instance_identity(str(instance_id))


def bootstrap(sdk, instance_id: str) -> dict:
    """Reports the instance up, as the instance (`sdk.platform.bootstrap_complete`), and says how it went: `{"state": "RUNNING"}`; or
    `{"state": null, "reason": ...}` when this process holds no credentials for it (rApp Management did not push them: no operator API base, or the
    rApp was unreachable), when rApp Management refused (`409`: the instance is not DEPLOYING, for example already RUNNING) or failed. Never raises:
    the rApp keeps serving the instance either way, and the answer tells the operator what is missing."""
    from ._common import SdkError
    if not is_bound(instance_id):
        return {"state": None, "reason": "NO_CREDENTIALS: rApp Management has not handed this rApp the instance's credentials"}
    try:
        return {"state": sdk.platform.bootstrap_complete(instance_id).get("state")}
    except SdkError as exc:
        title = exc.body.get("title") if isinstance(exc.body, dict) else None
        return {"state": None, "reason": f"{exc.status_code} {title or 'REFUSED'}"}


def _acting_for_instances(app):
    """ASGI middleware: a request for `/instances/{id}/...` acts for that instance, so the calls the handler makes carry its identity."""
    async def middleware(scope, receive, send):
        match = _INSTANCE_PATH.match(scope.get("path", "")) if scope.get("type") == "http" else None
        if match is None:
            await app(scope, receive, send)
            return
        token = set_acting_instance(match.group(1).lower())
        try:
            await app(scope, receive, send)
        finally:
            reset_acting_instance(token)
    return middleware


def install(app) -> None:
    """Adds to a FastAPI app the route rApp Management pushes an instance's credentials to, and the middleware that acts for the instance of each
    `/instances/{id}/...` request."""
    from fastapi import APIRouter, Header, HTTPException
    from pydantic import BaseModel

    class InstanceCredentials(BaseModel):
        """What rApp Management pushes: the instance's invoker id at SME and its secret."""
        oauthClientId: str
        oauthClientSecret: str

    router = APIRouter()

    @router.post("/instances/{instance_id}/credentials", status_code=204, include_in_schema=False)
    def receive_credentials(instance_id: str, body: InstanceCredentials, x_r1_role: str | None = Header(default=None, alias=ROLE_HEADER)):
        """Keeps the instance's credentials (a later push replaces them). Only an SMO module may push them, through R1 Termination, which stamps the
        caller's role (403 otherwise); a browser cannot, since the GUI backend forwards only the routes the package declares."""
        if x_r1_role != ROLE_INTERNAL:
            raise HTTPException(status_code=403, detail="the instance's credentials come from rApp Management only")
        bind_instance_identity(instance_id.lower(), body.oauthClientId, body.oauthClientSecret)

    app.include_router(router)
    app.add_middleware(_ASGIWrapper)


class _ASGIWrapper:
    """`app.add_middleware` takes a class; this one wraps the next ASGI app in `_acting_for_instances`."""

    def __init__(self, app):
        """Wraps `app`."""
        self._inner = _acting_for_instances(app)

    async def __call__(self, scope, receive, send):
        """Hands the request to the wrapped middleware."""
        await self._inner(scope, receive, send)
