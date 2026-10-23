"""sdk.analytics: a thin client over MDAF (`mdaf/`) and over RAN Analytics' producer registration route (`ran-analytics/`), TS 28.104 MDA NRM.

MDAF owns reports and subscriptions; `ran-analytics/` keeps only producer registration and is itself an MDAF consumer. The namespace mirrors the domain an rApp author thinks of as
"analytics", so producer registration is here even though it is a different backend service from the one reports and subscriptions live on (`docs/ARCHITECTURE.md`, MDAF).
Part of the SDK (`sdk/README.md`); one method per route, no validation of its own.
"""

import uuid

from ._common import BaseClient, ensure_ok


class AnalyticsClient(BaseClient):
    """The `sdk.analytics` namespace: analytics producers, reports and subscriptions, and the TS 28.104 MDARequest / MDAReport calls."""
    def register_producer(self, producer_id: str, analytics_type: str, dme_input_types: list[uuid.UUID | str],
                           output_schema: dict, mda_type: str | None = None) -> dict:
        """Register an analytics producer at RAN Analytics (`POST /ran-analytics/producers`). An rApp registers only itself: `producer_id` is its own invoker id (403
        PRODUCER_IDENTITY_MISMATCH otherwise), and it may publish reports only of the types it is registered for (call flow 08).

        `mda_type` is TS 28.104's closed MDAType enum, optional: omit it to let the route derive one for this build's unambiguous shorthand values (HISTORY.md section 7), or pass one of the 25 spec
        values for genuine conformance. The query parameters carry the ids and the JSON body carries the input types and the output schema.
        """
        return ensure_ok(self._r1.post(
            "/ran-analytics/producers",
            params={"producer_id": producer_id, "analytics_type": analytics_type, "mda_type": mda_type},
            json={"dme_input_types": [str(t) for t in dme_input_types], "output_schema": output_schema},
        ))

    def deregister_producer(self, producer_id: str, analytics_type: str) -> None:
        """Deregister a producer of `analytics_type` (`DELETE /ran-analytics/producers/{producer_id}`); RAN Analytics withdraws its SME service. An rApp deregisters only itself."""
        ensure_ok(self._r1.delete(f"/ran-analytics/producers/{producer_id}", params={"analytics_type": analytics_type}))

    def list_producers(self, analytics_type: str | None = None, producer_id: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/ran-analytics/producers", params={"analytics_type": analytics_type, "producer_id": producer_id}))

    def publish_report(self, analytics_type: str, output: dict, input_sources: list[uuid.UUID | str] | None = None,
                        scope: dict | None = None) -> dict:
        """Publish an analytics report of `analytics_type` to MDAF (`POST /mdaf/reports`); the body is `{output, input_sources, scope}` and `analytics_type` goes in the query."""
        return ensure_ok(self._r1.post(
            "/mdaf/reports", params={"analytics_type": analytics_type},
            json={"output": output, "input_sources": [str(s) for s in (input_sources or [])], "scope": scope},
        ))

    def query_reports(self, analytics_type: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/mdaf/reports", params={"analytics_type": analytics_type}))

    def subscribe(self, analytics_type: str, requested_by: str, notification_destination: str | None = None,
                  scope: dict | None = None, threshold_info: list[dict] | None = None) -> dict:
        """Subscribe to reports of `analytics_type` at MDAF (`POST /mdaf/subscriptions`).

        `analytics_type` and `requested_by` go in the query; `notification_destination`, `scope` and `threshold_info` go in the JSON body. Each `threshold_info` entry is TS 28.104
        ThresholdInfo: `{monitoredMDAOutputIE, thresholdDirection, thresholdValue, hysteresis?}`.
        """
        # The route takes an explicit body {"scope": ..., "thresholdInfo": ..., "notificationDestination": ...}:
        # FastAPI embeds body fields under their names once there is more than one,
        # so none of them is sent as a bare, unwrapped JSON value. `analytics_type`
        # and `requested_by` are plain strings and stay query parameters. Each
        # threshold_info dict is a TS 28.104 ThresholdInfo:
        # {monitoredMDAOutputIE, thresholdDirection, thresholdValue, hysteresis?}.
        # The notification destination is a body field, like the
        # notificationDestination of every other subscription-shaped resource.
        return ensure_ok(self._r1.post("/mdaf/subscriptions", params={
            "analytics_type": analytics_type, "requested_by": requested_by,
        }, json={"notificationDestination": notification_destination, "scope": scope, "thresholdInfo": threshold_info}))

    def unsubscribe(self, subscription_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/mdaf/subscriptions/{subscription_id}"))

    def list_subscriptions(self, analytics_type: str | None = None, requested_by: str | None = None) -> list[dict]:
        return ensure_ok(self._r1.get("/mdaf/subscriptions", params={"analytics_type": analytics_type, "requested_by": requested_by}))

    # ------------------------------------------------------------ Wave 5: TS 28.104 MDA NRM

    def create_mda_request(self, requested_mda_outputs: list[dict], reporting_method: str,
                           reporting_target: str | None = None, analytics_scope: dict | None = None,
                           mda_function_ref: uuid.UUID | str | None = None, requested_by: str | None = None,
                           start_time: str | None = None, stop_time: str | None = None) -> dict:
        """TS 28.104 MDARequest (`POST /mdaf/mda-requests`): `requested_mda_outputs` is a list of MDAOutputPerMDAType (`{mDAType, mDAOutputIEFilters?}`) and delivery is per `reporting_method` (NOTIFICATION, FILE or STREAMING); unset fields are left out of the body."""
        body = {"requestedMDAOutputs": requested_mda_outputs, "reportingMethod": reporting_method,
                "reportingTarget": reporting_target, "analyticsScope": analytics_scope,
                "mDAFunctionRef": str(mda_function_ref) if mda_function_ref else None, "requestedBy": requested_by,
                "startTime": start_time, "stopTime": stop_time}
        return ensure_ok(self._r1.post("/mdaf/mda-requests", json={k: v for k, v in body.items() if v is not None}))

    def delete_mda_request(self, request_id: uuid.UUID | str) -> None:
        ensure_ok(self._r1.delete(f"/mdaf/mda-requests/{request_id}"))

    def publish_mda_report(self, mda_outputs: list[dict], managed_entities: list[str] | None = None,
                           report_kind: str | None = None, mda_request_ref: uuid.UUID | str | None = None,
                           input_sources: list[uuid.UUID | str] | None = None) -> dict:
        """TS 28.104 MDAReport with typed mDAOutputs (`POST /mdaf/mda-reports`); `report_kind` is ANALYTICS, PREDICTION or DRIFT and is inferred by MDAF when omitted; unset fields are left out of the body."""
        body = {"mDAOutputs": mda_outputs, "managedEntitiesScope": managed_entities, "reportKind": report_kind,
                "mDARequestRef": str(mda_request_ref) if mda_request_ref else None,
                "inputSources": [str(s) for s in (input_sources or [])]}
        return ensure_ok(self._r1.post("/mdaf/mda-reports", json={k: v for k, v in body.items() if v is not None}))

    def query_mda_reports(self, mda_type: str | None = None, report_kind: str | None = None,
                          managed_entity: str | None = None, mda_request_id: uuid.UUID | str | None = None) -> list[dict]:
        """MDA reports, newest first, narrowed by the filters that are set (`GET /mdaf/mda-reports`).

        Unset filters are left out of the query because `httpx` would send None as an empty string, which the UUID-typed `mda_request_id` filter rejects with 422.
        """
        # unset filters are left out: httpx would send None as an empty
        # string, which the UUID-typed mda_request_id filter rejects (422)
        params = {"mda_type": mda_type, "report_kind": report_kind, "managed_entity": managed_entity,
                  "mda_request_id": str(mda_request_id) if mda_request_id else None}
        return ensure_ok(self._r1.get("/mdaf/mda-reports", params={k: v for k, v in params.items() if v is not None}))

    def get_prediction(self, managed_entity: str, pm_name: str | None = None) -> dict | None:
        """The latest PREDICTION report (the TrafficTrendReport of HISTORY.md W5-03, a TS 28.104 PREDICTIONS_PM_DATA report) for one managed entity, such as a cell's predicted PRB utilisation.

        With `pm_name`, returns just that PmPrediction (`{pmName, pmPredictedValue, ...}`) from the newest report, or None when that report has no such prediction; None as well when no report exists yet.
        Only the newest report is searched.
        """
        reports = self.query_mda_reports(report_kind="PREDICTION", managed_entity=managed_entity)
        if not reports:
            return None
        report = reports[0]  # newest first
        if pm_name is None:
            return report
        for output in report["attributes"]["mDAOutputs"]:
            output_list = output["mDAOutputList"]
            if isinstance(output_list, dict):
                for prediction in output_list.get("pmPredictions") or []:
                    if prediction["pmName"] == pm_name:
                        return prediction
        return None
