"""TS 28.111 fault supervision: the alarm model's closed lists, read from the vendored OpenAPI (`specs/5G_APIs/TS28111_FaultNrm.yaml`, v19.3.0).

Nothing here is typed in: every list is the enum of a schema of that file, so a new release of the file changes the dictionary with it.

  alarm_types()           AlarmType (TS 28.111 calls it the event type, after ITU-T X.733): COMMUNICATIONS_ALARM, QUALITY_OF_SERVICE_ALARM, ...
  perceived_severities()  PerceivedSeverity: INDETERMINATE, CRITICAL, MAJOR, MINOR, WARNING, CLEARED
  probable_causes()       the standardised strings of ProbableCause (Annex B, informative); a vendor string or an integer is also a valid cause
  trend_indications()     TrendIndication
  ack_states()            AckState
  notification_types()    AlarmNotificationTypes (notifyNewAlarm, ...)
  alarm_record_fields()   the members of AlarmRecord, in the file's order
"""

import functools

from ..registry import DEFAULT_SPEC_DIR, ClassRegistry

FILE = "TS28111_FaultNrm.yaml"


@functools.lru_cache(maxsize=1)
def _schemas() -> dict:
    """The `components.schemas` of the vendored file (read once)."""
    return ClassRegistry(DEFAULT_SPEC_DIR, class_files=()).document(FILE)["components"]["schemas"]


def _enum(name: str) -> tuple[str, ...]:
    """The enum of schema `name`, wherever it sits under `oneOf` / `anyOf` (ProbableCause nests it two levels down)."""
    pending = [_schemas()[name]]
    while pending:
        node = pending.pop()
        if "enum" in node:
            return tuple(value for value in node["enum"] if value is not None)
        pending.extend(branch for keyword in ("oneOf", "anyOf") for branch in node.get(keyword, []))
    raise LookupError(f"{FILE} has no enum for {name}")


def alarm_types() -> tuple[str, ...]:
    """AlarmType, the event types of TS 28.111."""
    return _enum("AlarmType")


def perceived_severities() -> tuple[str, ...]:
    """PerceivedSeverity."""
    return _enum("PerceivedSeverity")


def probable_causes() -> tuple[str, ...]:
    """The standardised probable causes."""
    return _enum("ProbableCause")


def trend_indications() -> tuple[str, ...]:
    """TrendIndication."""
    return _enum("TrendIndication")


def ack_states() -> tuple[str, ...]:
    """AckState."""
    return _enum("AckState")


def notification_types() -> tuple[str, ...]:
    """AlarmNotificationTypes."""
    return _enum("AlarmNotificationTypes")


def alarm_record_fields() -> tuple[str, ...]:
    """The members of an AlarmRecord."""
    return tuple(_schemas()["AlarmRecord"]["properties"])


def is_probable_cause(value: object) -> bool:
    """Whether `value` may be a probable cause: a standardised string, any other (vendor) string, or an integer (TS 28.111: the mapping of integers
    is vendor specific). Only `None`, booleans and other types are not."""
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
