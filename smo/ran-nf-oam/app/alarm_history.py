"""MGT-8.2: the history of every alarm, written where the alarm changes.

An alarm is raised, acknowledged, unacknowledged, cleared or re-graded in several places (`POST /alarms/ingest`, the VES receiver's raise,
update and clear, a failed O1 config job, the onboarding flow, the ack and clear routes). Rather than a call at each, two ORM listeners on
`Alarm` write an `AlarmHistory` row in the same flush as the change: `after_insert` a RAISED row, `after_update` one row per changed field
(ack state: ACKNOWLEDGED / UNACKNOWLEDGED, by `ack_user_id`; severity: CLEARED by `clear_user_id`, or SEVERITY_CHANGED). A new place that
changes an alarm through the ORM is recorded without knowing this module exists; a bulk SQL UPDATE that bypasses the ORM is not, and no
such statement touches these two fields today. Installed by importing this module (app/main.py does).

MSG-3.6: the same two listeners publish the alarm's raise and its clear on the event bus (`org.oran.smo.alarm.raised` / `.cleared`, smo_shared/events.py), and
its acknowledgement (`.acknowledged` / `.unacknowledged`, call flow 20b) in the same flush, with the element's region and tenant, so a reader that follows the feed hears of an alarm when it is committed.
"""

import datetime
import uuid

from smo_shared import events
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm.attributes import History

from .models import Alarm, AlarmHistory, ManagedEntity

SOURCE = "/smo/ran-nf-oam"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _write(connection: Connection, alarm: Alarm, kind: str, before: str | None, after: str | None, by: str | None) -> None:
    """One history row for `alarm`, at the alarm's own change time when it has one (so the history and `changedAt` agree), else now."""
    connection.execute(AlarmHistory.__table__.insert().values(
        history_id=uuid.uuid4(), alarm_id=alarm.alarm_id, at=alarm.changed_at or _now(), event=kind, from_value=before, to_value=after, by=by))


def _publish(connection: Connection, alarm: Alarm, change: str) -> None:
    """MSG-3.6: `org.oran.smo.alarm.<change>` about the alarm, with what a reader needs to act without reading it back, and its element's place."""
    place = connection.execute(select(ManagedEntity.region, ManagedEntity.tenant)
                               .where(ManagedEntity.managed_element_ref == alarm.managed_element_ref)).first()
    events.publish(connection, "alarm", change, SOURCE, str(alarm.alarm_id),
                   {"alarmId": str(alarm.alarm_id), "managedElementRef": alarm.managed_element_ref, "managedFunctionRef": alarm.managed_function_ref,
                    "severity": alarm.severity, "probableCause": alarm.probable_cause, "ackState": alarm.ack_state, "ackUserId": alarm.ack_user_id},
                   place[0] if place else None, place[1] if place else None)


@event.listens_for(Alarm, "after_insert")
def _raised(_mapper, connection: Connection, alarm: Alarm) -> None:
    """A new alarm: RAISED, to its severity, and its raise published (a new alarm raised already cleared is only recorded)."""
    _write(connection, alarm, "RAISED", None, alarm.severity, None)
    if alarm.severity != "cleared":
        _publish(connection, alarm, "raised")


def _change(alarm: Alarm, field: str) -> tuple[str | None, str | None] | None:
    """(before, after) of `field` in the flush being written, or None when it did not change."""
    history: History = inspect(alarm).attrs[field].history
    if not history.has_changes():
        return None
    before = history.deleted[0] if history.deleted else None
    after = history.added[0] if history.added else None
    return None if before == after else (before, after)


@event.listens_for(Alarm, "after_update")
def _changed(_mapper, connection: Connection, alarm: Alarm) -> None:
    """The ack state and the severity, each recorded when it changed: who acknowledged or cleared it is on the alarm itself."""
    ack = _change(alarm, "ack_state")
    if ack is not None:
        _write(connection, alarm, ack[1] or "UNACKNOWLEDGED", ack[0], ack[1], alarm.ack_user_id)
        _publish(connection, alarm, (ack[1] or "UNACKNOWLEDGED").lower())        # call flow 20b: org.oran.smo.alarm.acknowledged / .unacknowledged
    severity = _change(alarm, "severity")
    if severity is not None:
        if severity[1] == "cleared":
            _write(connection, alarm, "CLEARED", severity[0], severity[1], alarm.clear_user_id)
            _publish(connection, alarm, "cleared")
        else:
            _write(connection, alarm, "SEVERITY_CHANGED", severity[0], severity[1], None)
