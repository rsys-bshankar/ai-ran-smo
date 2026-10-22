"""sdk.data: a thin client over DME (`dme/`, R1AP clause 7) and over the read routes of RAN NF OAM an rApp needs before it acts (cell guards, alarms, capabilities, live configuration).

One method per DME route (`dme/app/main.py`), with no business logic and no client-side re-validation of what DME validates itself (schema checks, delivery-method commitments). Beyond the
routes it holds three pieces of client-side logic that are worth knowing before editing: the dataset convenience `get_dataset` (find a type by name, reuse or create the consumer's data job,
page the records), the O1-action mediation `mediate_action` (the same route as `PlatformClient.execute_action`), and `alarm_cell` / `AlarmScope`, which decide which cells a critical alarm
holds (HISTORY.md W10-alarm-cellref). Part of the SDK (`sdk/README.md`).
"""

import logging
import threading
import uuid

from smo_shared.r1_client import acting_for, acting_instance

from ._common import BaseClient, SdkError, credential_kwargs, ensure_ok


log = logging.getLogger(__name__)

_registered_as: dict[str, str | None] = {}   # producer id -> the instance this process registered it for (None: as the process); what `register_type` may take down again
_registered_lock = threading.Lock()


class DataClient(BaseClient):
    """The `sdk.data` namespace: DME types, producers, data jobs, offers, type subscriptions and the data-plane store; O1 action mediation; and RAN NF OAM inventory reads."""
    def own_invoker_id(self) -> str | None:
        """The invoker id this client's calls carry (the instance's when called while acting for one, else the process's), or None when it cannot be told.

        DME accepts a producer registration from an rApp only under a `producerId` equal to this id (SEC-15.10b), so an rApp that registers a type passes it as `producer_id`.
        """
        return self._r1.acting_invoker_id()

    def register_type(self, namespace: str, name: str, version: str, type_name: str, producer_id: str,
                       data_production_schema: dict, producer_health_callback_url: str, job_callback_url: str,
                       collection_spec: dict | None = None, source_domain: str | None = None,
                       source_context: dict | None = None) -> dict:
        """Register a producer and the DME type it produces (`POST /dme/production-capabilities`).

        `producer_id` must be the caller's own invoker id (`own_invoker_id()`): DME refuses an rApp that names another (403 `DME_PRODUCER_NOT_OWNER`, SEC-15.10b).

        `source_domain` and `source_context` (optional) carry the provenance, LIVE_RAN or DIGITAL_TWIN, that drives the rule excluding Digital-Twin data from inference (`docs/ARCHITECTURE.md`, DME).
        """
        registered = ensure_ok(self._r1.post("/dme/production-capabilities", json={
            "namespace": namespace, "name": name, "version": version, "typeName": type_name,
            "producerId": producer_id, "dataProductionSchema": data_production_schema,
            "collectionSpec": collection_spec, "producerHealthCallbackUrl": producer_health_callback_url,
            "jobCallbackUrl": job_callback_url, "sourceDomain": source_domain, "sourceContext": source_context,
        }))
        with _registered_lock:
            _registered_as[producer_id] = acting_instance()
        self._drop_other_producers_of(producer_id, job_callback_url)
        return registered

    def _drop_other_producers_of(self, producer_id: str, job_callback_url: str) -> None:
        """Leaves `job_callback_url` with one producer: deregisters the others this process registered for it under another identity (the process's own, or an instance's).

        An rApp acts under its process identity and under the identity of each instance, and DME takes a producer only under the caller's own invoker id (SEC-15.10b), so the same
        rApp registering from both would leave DME holding two producers with the same callback and posting each job start to it twice. Only a producer registered by this process
        (or named by the process's own invoker id) is touched, each under the identity that registered it; one that cannot be deregistered (403, or its instance's identity is gone)
        is left, since rApp Management tears an instance's registration down when it ends. Registered first, deregistered after, so the type never goes DISABLED in between.
        """
        with acting_for(None):
            process_id = self._r1.acting_invoker_id()
        for other in self.list_producers():
            other_id = str(other.get("producerId"))
            if other_id == producer_id or other.get("jobCallbackUrl") != job_callback_url:
                continue
            with _registered_lock:
                known = other_id in _registered_as
                instance = _registered_as.get(other_id)
            if not known and other_id != process_id:
                continue
            try:
                with acting_for(instance):
                    self.deregister_producer(other_id)
            except SdkError as exc:
                log.warning("could not deregister the producer %s that shares the callback %s with %s: %s", other_id, job_callback_url, producer_id, exc)
                continue
            with _registered_lock:
                _registered_as.pop(other_id, None)

    def discover_types(self, data_category: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/dme/dme-types", params={"data_category": data_category}))

    def list_producers(self) -> list[dict]:
        """The registered producers (the ICS `GET /data-producer/v1/info-producers`); a producer is its own entity, not implied by a DME type row."""
        return ensure_ok(self._r1.get("/dme/production-capabilities"))

    def get_producer(self, producer_id: str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/production-capabilities/{producer_id}"))

    def deregister_producer(self, producer_id: str) -> None:
        """Remove this producer and its type links only; DME tells it to stop each job of those types, and a type left with no producer keeps its jobs, NO_PRODUCER
        until a producer joins (call flow 11d). Deleting a type is the operator's (the console) or an SMO module's, not an rApp's."""
        ensure_ok(self._r1.delete("/dme/production-capabilities", params={"producer_id": producer_id}))

    def query_producer_status(self, producer_id: str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/production-capabilities/{producer_id}/status"))

    def create_data_job(self, dme_type_id: uuid.UUID | str, data_delivery_mode: str, data_delivery_method: str,
                         consumer_id: str, production_job_definition: dict | None = None, delivery_details: dict | None = None,
                         lifecycle_stage: str | None = None) -> dict:
        """Create a data job of a DME type for a consumer (`POST /dme/data-jobs`).

        `lifecycle_stage` is TRAINING, TESTING, EMULATION, INFERENCE or CLOSED_LOOP_FEEDBACK (or None); DME answers 422 (raised as `SdkError`) for an INFERENCE job on a type whose source is a DIGITAL_TWIN.
        """
        return ensure_ok(self._r1.post("/dme/data-jobs", json={
            "dataDeliveryMode": data_delivery_mode, "dmeTypeId": str(dme_type_id),
            "productionJobDefinition": production_job_definition or {}, "dataDeliveryMethod": data_delivery_method,
            "deliveryDetails": delivery_details or {}, "consumerId": consumer_id, "lifecycleStage": lifecycle_stage,
        }))

    def get_data_job(self, data_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/data-jobs/{data_job_id}"))

    def update_data_job(self, data_job_id: uuid.UUID | str, dme_type_id: uuid.UUID | str, data_delivery_mode: str,
                         data_delivery_method: str, consumer_id: str, production_job_definition: dict | None = None,
                         delivery_details: dict | None = None) -> dict:
        """Replace a data job's definition (`PUT /dme/data-jobs/{id}`); every field of the job is sent again, and the lifecycle stage is not among them."""
        return ensure_ok(self._r1.put(f"/dme/data-jobs/{data_job_id}", json={
            "dataDeliveryMode": data_delivery_mode, "dmeTypeId": str(dme_type_id),
            "productionJobDefinition": production_job_definition or {}, "dataDeliveryMethod": data_delivery_method,
            "deliveryDetails": delivery_details or {}, "consumerId": consumer_id,
        }))

    def query_data_job_status(self, data_job_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/data-jobs/{data_job_id}/status"))

    def terminate_data_job(self, data_job_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/dme/data-jobs/{data_job_id}"))

    def terminate_data_jobs_for_consumer(self, consumer_id: str) -> None:
        """The ICS deleteJobsForOwner (`ics-api.yaml`): tear down every job this consumer owns in one call rather than one `terminate_data_job` per id."""
        ensure_ok(self._r1.delete("/dme/data-jobs", params={"consumer_id": consumer_id}))

    def list_data_jobs(self, dme_type_id: uuid.UUID | str | None = None, consumer_id: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/dme/data-jobs", params={"dme_type_id": dme_type_id, "consumer_id": consumer_id}))

    def create_data_offer(self, dme_type_id: uuid.UUID | str, data_delivery_mode: str, data_delivery_methods: list[str],
                           data_offer_termination_notification_uri: str, production_job_definition: dict | None = None,
                           data_availability_notification_uri: str | None = None) -> dict:
        """Create a data offer of a DME type (`POST /dme/offers`) with the delivery methods offered; the termination notification URI is required, the availability one optional."""
        return ensure_ok(self._r1.post("/dme/offers", json={
            "dmeTypeId": str(dme_type_id), "dataDeliveryMode": data_delivery_mode,
            "productionJobDefinition": production_job_definition or {}, "dataDeliveryMethods": data_delivery_methods,
            "dataAvailabilityNotificationUri": data_availability_notification_uri,
            "dataOfferTerminationNotificationUri": data_offer_termination_notification_uri,
        }))

    def get_data_offer(self, offer_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/offers/{offer_id}"))

    def terminate_data_offer(self, offer_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/dme/offers/{offer_id}"))

    def list_data_offers(self, dme_type_id: uuid.UUID | str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/dme/offers", params={"dme_type_id": dme_type_id}))

    def notify_data_available(self, offer_id: uuid.UUID | str, payload: dict) -> None:
        ensure_ok(self._r1.post(f"/dme/offers/{offer_id}/notify", json=payload))

    def subscribe_type_changes(self, notification_destination: str, owner: str) -> dict:
        return ensure_ok(self._r1.post("/dme/type-subscriptions", json={
            "notificationDestination": notification_destination, "owner": owner,
        }))

    def list_type_subscriptions(self, owner: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/dme/type-subscriptions", params={"owner": owner}))

    def get_type_subscription(self, subscription_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/type-subscriptions/{subscription_id}"))

    def unsubscribe_type_changes(self, subscription_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/dme/type-subscriptions/{subscription_id}"))

    # ---------------------------------------------------------------- Wave 3: real data-plane store
    # docs/ARCHITECTURE.md (DME) — a producer ingests, a consumer
    # (rApp or MDAF, no distinction here) fetches. Not restricted to
    # either caller.

    def ingest_data_record(self, data_job_id: uuid.UUID | str, payload: dict) -> dict:
        """A producer stores one record into a data job's store (`POST /dme/data-jobs/{id}/records`, body `{payload}`); not restricted to either caller."""
        return ensure_ok(self._r1.post(f"/dme/data-jobs/{data_job_id}/records", json={"payload": payload}))

    def fetch_data_records(self, data_job_id: uuid.UUID | str, limit: int = 100) -> list[dict]:
        """A consumer (an rApp or MDAF, no distinction here) reads up to `limit` records of a data job, in the order the route returns them (`GET /dme/data-jobs/{id}/records`)."""
        return ensure_ok(self._r1.get(f"/dme/data-jobs/{data_job_id}/records", params={"limit": limit}))

    # ---------------------------------------------------------------- Wave 3: O1 action mediation
    # docs/ARCHITECTURE.md (DME) — DME mediates and forwards to
    # ran-nf-oam's real NETCONF dispatch; it doesn't speak O1 itself.

    def mediate_action(self, requested_by: str, changes: list[dict], scope: str = "single-ME",
                        msac_role: str | None = None, source_context: dict | None = None, decision: dict | None = None,
                        *, msac_credential: str | None = None) -> dict:
        """Submit an O1 configuration action to DME, which mediates it to RAN NF OAM's NETCONF dispatch (`POST /dme/actions`); DME does not speak O1 itself.

        `msac_credential` (keyword only, `SEC-15.6b`) is the credential of the MSAC Identity named by `requested_by`, sent in the `X-MSAC-Credential` header and only when given; it is never put in the body or
        in a log line or message. RAN NF OAM asks an rApp for it when the Identity has one (403 `MSAC_ACCESS_DENIED` without it). DME passes the header on to RAN NF OAM, which asks an rApp for it.

        The same route as `PlatformClient.execute_action`, without its `action_id` idempotency key and without the approval hint; `decision` (PR-AI-13, why the rApp acts) is sent only when given.
        Returns the action record; raises `SdkError` for the route's refusals.
        """
        payload = {"requestedBy": requested_by, "changes": changes, "scope": scope, "msacRole": msac_role, "sourceContext": source_context}
        if decision is not None:                       # PR-AI-13: why the rApp acts (see Platform.execute_action)
            payload["decision"] = decision
        return ensure_ok(self._r1.post("/dme/actions", json=payload, **credential_kwargs(msac_credential)))

    def get_action(self, action_id: uuid.UUID | str) -> dict:
        return ensure_ok(self._r1.get(f"/dme/actions/{action_id}"))

    def list_actions(self, managed_element_ref: str | None = None, requested_by: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/dme/actions", params={"managed_element_ref": managed_element_ref, "requested_by": requested_by}))

    # ---------------------------------------------------------------- Wave 9: RAN inventory reads (RAN NF OAM)
    # HISTORY.md W9-01..06 — what an rApp needs to know
    # before deciding an O1 action: a cell's guard attributes (D-5), and the
    # managed element's vendor capabilities.

    def query_cell_guards(self, managed_element_ref: str | None = None, cell_id: str | None = None,
                          cell_class: str | None = None, sector_group: str | None = None,
                          incident_zone: str | None = None) -> list[dict]:
        """The cells' guard attributes (HISTORY.md W9-01..06, D-5) an rApp consults before deciding an O1 action; each filter that is set narrows the list.

        Unset filters are left out of the query (not sent as None), because `httpx` would send None as an empty string, which the enum-typed `cell_class` filter rejects with 422.
        """
        # unset filters are left out: httpx would send None as an empty
        # string, which the enum-typed cell_class filter rejects
        params = {"managed_element_ref": managed_element_ref, "cell_id": cell_id, "cell_class": cell_class,
                  "sector_group": sector_group, "incident_zone": incident_zone}
        return ensure_ok(self._r1.get("/ran-nf-oam/cell-guards", params={k: v for k, v in params.items() if v is not None}))

    def query_critical_alarms(self, managed_element_ref: str) -> "AlarmScope":
        """The managed element's active critical alarms, wrapped in an `AlarmScope` that answers which of them hold which cells (HISTORY.md W10-alarm-cellref).

        Reads the first 500 critical alarms of the element only (no paging).
        """
        resp = self._r1.get("/ran-nf-oam/alarms", params={"managed_element_ref": managed_element_ref,
                                                          "severity": "critical", "limit": 500})
        return AlarmScope(ensure_ok(resp))

    def get_managed_entity(self, managed_element_ref: str) -> dict:
        """The managed element as RAN NF OAM holds it (`GET /ran-nf-oam/managed-entities/{ref}`)."""
        return ensure_ok(self._r1.get(f"/ran-nf-oam/managed-entities/{managed_element_ref}"))

    def get_vendor_capability(self, vendor_name: str) -> dict:
        return ensure_ok(self._r1.get(f"/ran-nf-oam/vendor-capabilities/{vendor_name}"))

    def get_o1_capabilities(self) -> dict:
        return ensure_ok(self._r1.get("/ran-nf-oam/capabilities"))

    # ---------------------------------------------------------------- Wave 10.1: dataset access and O1 read-back
    # W10-03 (decision D-4): convenience calls named as in the Wave 10
    # documents, over the existing DME / RAN NF OAM routes.

    def get_dataset(self, name: str, consumer_id: str, namespace: str | None = None, lifecycle_stage: str | None = None,
                    max_records: int = 2000) -> dict:
        """The records of a dataset for a consumer, as `{dmeTypeId, dataJobId, sourceDomain, records}` with the records oldest first.

        A dataset is a DME type found by its name, its type name or its leaf name (RAN NF OAM's PM counter types are `PMCounters.<counter>`, so "PRB_UTILIZATION" finds
        `PMCounters.PRB_UTILIZATION`), searched within `namespace` when given. The consumer's data job for the same `lifecycle_stage` is reused if it exists, else created (CONTINUOUS, PULL_HTTP);
        so a call can create a job as a side effect. Records are read newest first, 500 at a time up to `max_records`, each page before the oldest one read so far. Raises `SdkError(404)` when no type matches. Design record: `HISTORY.md` W10-03 (D-4).
        """
        dme_type = next((t for t in self.discover_types(namespace)
                         if name in (t["dmeTypeIdStruct"]["name"], t["typeName"])
                         or t["dmeTypeIdStruct"]["name"].rsplit(".", 1)[-1] == name), None)
        if dme_type is None:
            raise SdkError(404, {"title": "DME_TYPE_NOT_FOUND", "detail": f"no dataset {name!r}"})
        type_id = dme_type["dmeTypeId"]
        job = next((j for j in self.list_data_jobs(dme_type_id=type_id, consumer_id=consumer_id)
                    if j.get("lifecycleStage") == lifecycle_stage), None)
        job_id = job["dataJobId"] if job else self.create_data_job(
            type_id, "CONTINUOUS", "PULL_HTTP", consumer_id, lifecycle_stage=lifecycle_stage)["dataJobId"]
        records: list[dict] = []
        params: dict = {"limit": 500}
        while len(records) < max_records:
            # call flow 12b: page with `before` (the oldest record read so far), so records that arrive while reading do not shift the pages (an offset did)
            page = ensure_ok(self._r1.get(f"/dme/data-jobs/{job_id}/records", params=params))
            records.extend(page)
            if len(page) < 500:
                break
            params = {"limit": 500, "before": page[-1]["recordId"]}
        return {"dmeTypeId": type_id, "dataJobId": job_id, "sourceDomain": dme_type.get("sourceDomain"),
                "records": list(reversed(records[:max_records]))}

    def read_config(self, managed_element_ref: str, managed_function_ref: str | None = None) -> dict:
        """The managed object's live running configuration (NETCONF get-config through RAN NF OAM), for read-after-write verification of an action."""
        return ensure_ok(self._r1.get(f"/ran-nf-oam/managed-entities/{managed_element_ref}/config",
                                      params={"managed_function_ref": managed_function_ref}))


# ---------------------------------------------------------------- W10-alarm-cellref: which cells an alarm holds

# IOCs addressed per cell (`<IOC>=<cellId>`) and per relation of a cell
# (`<IOC>=<cellId>-<other>`): an alarm on one of them is about that cell.
# The IOC names an alarm's managedFunctionRef can start with when the alarm is about one cell; an IOC listed in neither set makes the alarm element-wide, so it holds every cell.
CELL_IOCS = frozenset({"NRCellDU", "NRCellCU", "NRSectorCarrier", "CommonBeamformingFunction", "CESManagementFunction"})
RELATION_IOCS = frozenset({"NRCellRelation", "NRFreqRelation"})


def alarm_cell(alarm: dict) -> str | None:
    """The cell an alarm is about, read from its `managedFunctionRef`; None when it names no cell.

    None for no ref, a malformed ref, or a function of the element as a whole such as `DMROFunction`; such an alarm holds the whole managed element. A ref `<IOC>=<cellId>` of a cell IOC gives the
    cell id, and `<IOC>=<cellId>-<other>` of a relation IOC gives the cell id before the first dash.
    """
    ioc, sep, instance = (alarm.get("managedFunctionRef") or "").partition("=")
    if not sep or not instance:
        return None
    if ioc in CELL_IOCS:
        return instance
    if ioc in RELATION_IOCS:
        return instance.split("-", 1)[0] or None
    return None


class AlarmScope:
    """A managed element's critical alarms, by the cells they hold.

    An alarm that names no cell holds every cell (`element_wide`); one that names a cell holds that cell only. Callers decide whether a cell's neighbours count by passing them to `holding`.
    """

    def __init__(self, alarms: list[dict]):
        """Keep only the alarms whose severity is `critical` (the list may hold others) and split off the ones that name no cell."""
        self.alarms = [a for a in alarms if a.get("severity") == "critical"]
        self.element_wide = [a for a in self.alarms if alarm_cell(a) is None]

    def holding(self, cells) -> list[dict]:
        """The alarms that hold any of `cells`: every element-wide alarm, plus those raised on one of the cells."""
        wanted = set(cells)
        return self.element_wide + [a for a in self.alarms if (cell := alarm_cell(a)) is not None and cell in wanted]

    def ids_holding(self, cells) -> list[str]:
        return [a["alarmId"] for a in self.holding(cells)]
