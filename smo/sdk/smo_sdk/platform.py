"""sdk.platform: a thin client over SME (`sme/`)'s generic, domain-independent surface (provider enrolment, service publish and discovery, CAPIF event subscriptions) and over the DME
O1-action route with its approval read-back.

Deliberately narrow: SME's OAuth2 token and introspection routes and its Trusted Invokers routes are `R1Client`'s concern (invoker onboarding and token acquisition happen transparently on every
call; see `shared/smo_shared/r1_client.py`), not something an rApp author touches. Part of the SDK (`sdk/README.md`); one method per route, no validation of its own. Every change the methods
make has to be one the gateway lets an rApp make (`smo_shared/roles.py`, `RAPP_MAY_CHANGE`), which `tests/conftest.py` asserts on each recorded call.
"""

import uuid

from ._common import BaseClient, credential_kwargs, ensure_ok


class PlatformClient(BaseClient):
    """The `sdk.platform` namespace: provider registration, service publish and discovery and event subscriptions at SME, and `execute_action` / `get_approval` for O1 actions."""
    def bootstrap_complete(self, instance_id: str) -> dict:
        """Tells rApp Management that the instance is up (`POST /rapp-mgmt/instances/{id}/bootstrap-complete`): it puts the instance's limits and
        approval policy in force, registers its SME declarations and moves it DEPLOYING -> RUNNING. Made as the instance (its credentials, bound
        with `smo_sdk.instances`); rApp Management refuses another caller's (403 NOT_THIS_INSTANCE) and an instance not DEPLOYING (409)."""
        return ensure_ok(self._r1.post(f"/rapp-mgmt/instances/{instance_id}/bootstrap-complete"))

    def report_fault(self, instance_id: str, severity: str, description: str = "") -> dict:
        """Reports a fault of the instance (`POST /rapp-mgmt/instances/{id}/fault`), as the instance. `severity="critical"` moves a RUNNING instance to
        FAULTED and one that is not up yet (DEPLOYING) to FAULTED as a failed bootstrap; the pending replacement of an upgrade rolls the upgrade back
        instead (call flow 07a, 07b). Any other severity is only recorded. rApp Management refuses another caller's instance (403 NOT_THIS_INSTANCE)."""
        return ensure_ok(self._r1.post(f"/rapp-mgmt/instances/{instance_id}/fault", params={"severity": severity, "description": description}))

    def register_provider(self, apf_id: str, provider_domain_info: str | None = None) -> dict:
        return ensure_ok(self._r1.post("/sme/provider-registrations", json={
            "apfId": apf_id, "providerDomainInfo": provider_domain_info,
        }))

    def deregister_provider(self, apf_id: str) -> None:
        ensure_ok(self._r1.delete(f"/sme/provider-registrations/{apf_id}"))

    def publish_service(self, apf_id: str, service_name: str, producer_id: str, endpoint: str, version: str,
                         full_api_versions: list[str] | None = None, service_capabilities: dict | None = None,
                         selection_criteria: dict | None = None, module_scope: str = "", allowed_consumers: list[str] | None = None,
                         aef_profiles: list[dict] | None = None, api_supp_feats: str | None = None,
                         shareable_info: dict | None = None) -> dict:
        """Publish a service API under the provider `apf_id` at SME (`POST /sme/published-apis/v1/{apf_id}/service-apis`).

        Returns the stored service record. The optional lists and dicts default to empty (never None) in the body, while `api_supp_feats` and `shareable_info` are sent as null when unset.
        Raises `SdkError` with the route's status (for example a name conflict is a final 409, not retried).
        """
        return ensure_ok(self._r1.post(f"/sme/published-apis/v1/{apf_id}/service-apis", json={
            "serviceName": service_name, "producerId": producer_id, "endpoint": endpoint, "version": version,
            "fullApiVersions": full_api_versions or [], "serviceCapabilities": service_capabilities or {},
            "selectionCriteria": selection_criteria or {}, "moduleScope": module_scope,
            "allowedConsumers": allowed_consumers or [], "aefProfiles": aef_profiles or [],
            "apiSuppFeats": api_supp_feats, "shareableInfo": shareable_info,
        }))

    def list_published_services(self, apf_id: str) -> list[dict]:
        return ensure_ok(self._r1.get(f"/sme/published-apis/v1/{apf_id}/service-apis"))

    def unpublish_service(self, apf_id: str, service_id: str) -> None:
        ensure_ok(self._r1.delete(f"/sme/published-apis/v1/{apf_id}/service-apis/{service_id}"))

    def discover_services(self, api_invoker_id: str | None = None, api_name: str | None = None,
                           api_version: str | None = None, aef_id: str | None = None, protocol: str | None = None,
                           data_format: str | None = None, comm_type: str | None = None) -> list[dict]:
        """Discover published service APIs (`GET /sme/service-apis/v1/allServiceAPIs`); each filter that is set narrows the result and an unset one is left out of the query by `httpx`."""
        return ensure_ok(self._r1.get("/sme/service-apis/v1/allServiceAPIs", params={
            "api_invoker_id": api_invoker_id, "api_name": api_name, "api_version": api_version, "aef_id": aef_id,
            "protocol": protocol, "data_format": data_format, "comm_type": comm_type,
        }))

    def subscribe_to_events(self, subscriber_id: str, event_types: list[str], callback_uri: str,
                             api_ids: list[str] | None = None, api_invoker_ids: list[str] | None = None,
                             aef_ids: list[str] | None = None) -> dict:
        """CAPIF event subscription; each filter that is set must match the event."""
        return ensure_ok(self._r1.post(f"/sme/capif-events/v1/{subscriber_id}/subscriptions", json={
            "subscriberId": subscriber_id, "eventTypes": event_types, "callbackUri": callback_uri, "apiIds": api_ids,
            "apiInvokerIds": api_invoker_ids, "aefIds": aef_ids,
        }))

    def list_event_subscriptions(self, subscriber_id: str) -> list[dict]:
        return ensure_ok(self._r1.get(f"/sme/capif-events/v1/{subscriber_id}/subscriptions"))

    def unsubscribe_from_events(self, subscriber_id: str, subscription_id: str) -> None:
        ensure_ok(self._r1.delete(f"/sme/capif-events/v1/{subscriber_id}/subscriptions/{subscription_id}"))

    # ---------------------------------------------------------------- Wave 10.1: O1 actions
    def execute_action(self, requested_by: str, changes: list[dict], action_id: uuid.UUID | str | None = None,
                       source_context: dict | None = None, scope: str = "single-ME", msac_role: str | None = None,
                       decision: dict | None = None, *, msac_credential: str | None = None) -> dict:
        """Submit an O1 configuration action, mediated by DME (`POST /dme/actions`, then RAN NF OAM and NETCONF).

        `action_id` is an idempotency key: DME ignores a re-sent id and never applies the action twice. `decision` (PR-AI-13) is why the rApp acts, kept by RAN NF OAM as the decision record of
        the job: `{"inputsRef": ..., "modelVersion": ..., "rationale": ...}`, each optional (a reference to the inputs, never the data). When the operator holds this rApp's writes for approval
        (PR-AI-11) the answer has `status` `PENDING_APPROVAL`, no `forwardedJobId` and an `approvalId`; follow it with `get_approval`. Design record: `HISTORY.md` W10-03 (decision D-4).

        `msac_credential` (keyword only, `SEC-15.6b`) is the credential of the MSAC Identity named by `requested_by`, sent in the `X-MSAC-Credential` header and only when given; it is never put in the body or
        in a log line or message. RAN NF OAM asks an rApp for it when the Identity has one (403 `MSAC_ACCESS_DENIED` without it). DME passes the header on to RAN NF OAM, which asks an rApp for it.
        """
        payload = {"requestedBy": requested_by, "changes": changes, "scope": scope, "msacRole": msac_role,
                   "sourceContext": source_context, "actionId": str(action_id) if action_id else None}
        if decision is not None:
            payload["decision"] = decision
        return ensure_ok(self._r1.post("/dme/actions", json=payload, **credential_kwargs(msac_credential)))

    def get_approval(self, approval_id: uuid.UUID | str) -> dict:
        """PR-AI-11: the state of an action held for a human: `status` is PENDING, APPROVED (then `jobId` is the config job), REJECTED, EXPIRED or REFUSED."""
        return ensure_ok(self._r1.get(f"/ran-nf-oam/rapp-approvals/{approval_id}"))
