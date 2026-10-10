"""MGT-8.2: the history of every alarm, written where the alarm changes.

An alarm is raised, acknowledged, unacknowledged, cleared or re-graded in several places (`POST /alarms/ingest`, the VES receiver's raise,
update and clear, a failed O1 config job, the onboarding flow, the ack and clear routes). Rather than a call at each, two ORM listeners on
`Alarm` write an `AlarmHistory` row in the same flush as the change: `after_insert` a RAISED row, `after_update` one row per changed field
(ack state: ACKNOWLEDGED / UNACKNOWLEDGED, by `ack_user_id`; severity: CLEARED by `clear_user_id`, or SEVERITY_CHANGED). A new place that
changes an alarm through the ORM is recorded without knowing this module exists; a bulk SQL UPDATE that bypasses the ORM is not, and no
such statement touches these two fields today. Installed by importing this module (app/main.py does).
"""

import datetime
import uuid

from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.orm.attributes import History

from .models import Alarm, AlarmHistory


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _write(connection: Connection, alarm: Alarm, kind: str, before: str | None, after: str | None, by: str | None) -> None:
    """One history row for `alarm`, at the alarm's own change time when it has one (so the history and `changedAt` agree), else now."""
    connection.execute(AlarmHistory.__table__.insert().values(
        history_id=uuid.uuid4(), alarm_id=alarm.alarm_id, at=alarm.changed_at or _now(), event=kind, from_value=before, to_value=after, by=by))


@event.listens_for(Alarm, "after_insert")
def _raised(_mapper, connection: Connection, alarm: Alarm) -> None:
    """A new alarm: RAISED, to its severity."""
    _write(connection, alarm, "RAISED", None, alarm.severity, None)


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
    severity = _change(alarm, "severity")
    if severity is not None:
        if severity[1] == "cleared":
            _write(connection, alarm, "CLEARED", severity[0], severity[1], alarm.clear_user_id)
        else:
            _write(connection, alarm, "SEVERITY_CHANGED", severity[0], severity[1], None)
