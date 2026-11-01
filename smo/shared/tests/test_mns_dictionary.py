"""The Component-C dictionary (smo_shared/mns/dictionary/, PR-CONV-S1.3): the TS 28.111 lists read from the vendored file, the TS 28.552 counters and
TS 28.554 KPIs held as data (a golden snapshot, so a change to either is a visible diff here), the lookup API and the formula evaluator's grammar.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_mns_dictionary.py -q
"""

import pytest

from smo_shared.mns import dictionary
from smo_shared.mns.dictionary import ts28111

# Golden: every counter as (name, clause, unit, measured on), in the data's order.
COUNTERS = [
    ('DRB.AirIfDelayDl', '5.1.1.1.1', '0.1 ms', 'NRCellDU'),
    ('DRB.RlcSduDelayDl', '5.1.1.1.2', '0.1 ms', 'NRCellDU'),
    ('RRU.PrbTotDl', '5.1.1.2.1', '%', 'NRCellDU'),
    ('RRU.PrbTotUl', '5.1.1.2.2', '%', 'NRCellDU'),
    ('RRU.PrbUsedDl', '5.1.1.2.5', 'PRBs', 'NRCellDU'),
    ('RRU.PrbAvailDl', '5.1.1.2.6', 'PRBs', 'NRCellDU'),
    ('RRU.PrbUsedUl', '5.1.1.2.7', 'PRBs', 'NRCellDU'),
    ('RRU.PrbAvailUl', '5.1.1.2.8', 'PRBs', 'NRCellDU'),
    ('DRB.UEThpDl', '5.1.1.3.1', 'kbit/s', 'NRCellDU'),
    ('DRB.UEThpUl', '5.1.1.3.3', 'kbit/s', 'NRCellDU'),
    ('RRC.ConnMean', '5.1.1.4.1', 'connections', 'NRCellCU'),
    ('RRC.ConnMax', '5.1.1.4.2', 'connections', 'NRCellCU'),
    ('RRC.InactiveConnMean', '5.1.1.4.3', 'connections', 'NRCellCU'),
    ('RRC.InactiveConnMax', '5.1.1.4.4', 'connections', 'NRCellCU'),
    ('MM.HoPrepInterReq', '5.1.1.6.1.1', 'attempts', 'GNBCUCPFunction'),
    ('MM.HoPrepInterSucc', '5.1.1.6.1.2', 'successes', 'GNBCUCPFunction'),
    ('MM.HoPrepInterFail.cause', '5.1.1.6.1.3', 'failures', 'GNBCUCPFunction'),
    ('MM.HoResAlloInterReq', '5.1.1.6.1.4', 'attempts', 'GNBCUCPFunction'),
    ('MM.HoResAlloInterSucc', '5.1.1.6.1.5', 'successes', 'GNBCUCPFunction'),
    ('MM.HoExeInterReq', '5.1.1.6.1.7', 'attempts', 'GNBCUCPFunction'),
    ('MM.HoExeInterSucc', '5.1.1.6.1.8', 'successes', 'GNBCUCPFunction'),
    ('MM.HoExeIntraReq', '5.1.1.6.2.1', 'attempts', 'GNBCUCPFunction'),
    ('MM.HoExeIntraSucc', '5.1.1.6.2.2', 'successes', 'GNBCUCPFunction'),
    ('DRB.EstabAtt.5QI', '5.1.1.10.1', 'attempts', 'NRCellCU'),
    ('DRB.EstabSucc.5QI', '5.1.1.10.2', 'successes', 'NRCellCU'),
    ('RRC.ConnEstabAtt.Cause', '5.1.1.15.1', 'attempts', 'NRCellCU'),
    ('RRC.ConnEstabSucc.Cause', '5.1.1.15.2', 'successes', 'NRCellCU'),
    ('PEE.AvgPower', '5.1.1.19.1', 'W', 'ManagedElement'),
    ('PEE.MinPower', '5.1.1.19.2', 'W', 'ManagedElement'),
    ('PEE.MaxPower', '5.1.1.19.3', 'W', 'ManagedElement'),
    ('PEE.Energy', '5.1.1.19.4', 'kWh', 'ManagedElement'),
    ('PEE.AvgTemperature', '5.1.1.19.5', 'degree Celsius', 'ManagedElement'),
    ('PEE.MinTemperature', '5.1.1.19.6', 'degree Celsius', 'ManagedElement'),
    ('PEE.MaxTemperature', '5.1.1.19.7', 'degree Celsius', 'ManagedElement'),
    ('PEE.Voltage', '5.1.1.19.8', 'V', 'ManagedElement'),
    ('PEE.Current', '5.1.1.19.9', 'A', 'ManagedElement'),
    ('PEE.Humidity', '5.1.1.19.10', '%', 'ManagedElement'),
    ('DRB.PdcpSduVolumeDL', '5.1.2.1.1', 'Mbit', 'NRCellCU'),
    ('DRB.PdcpSduVolumeUL', '5.1.2.1.2', 'Mbit', 'NRCellCU'),
]

KPIS = [("EE.DataVolume", "6.7.1", "bit/J"), ("DRBAccessibility", "6.2.2", "%"), ("InterGnbHoSuccessRate", "6.5.1", "%"), ("DlUeThroughput", "6.3.1", "kbit/s")]


# ---- TS 28.111, read from specs/5G_APIs/TS28111_FaultNrm.yaml

def test_the_alarm_lists_are_the_vendored_enums():
    """Event types, severities, trend, ack states and notifications are the vendored file's enums, in its order."""
    assert ts28111.alarm_types() == ("COMMUNICATIONS_ALARM", "QUALITY_OF_SERVICE_ALARM", "PROCESSING_ERROR_ALARM", "EQUIPMENT_ALARM",
                                     "ENVIRONMENTAL_ALARM", "INTEGRITY_VIOLATION", "OPERATIONAL_VIOLATION", "PHYSICAL_VIOLATION",
                                     "SECURITY_SERVICE_OR_MECHANISM_VIOLATION", "TIME_DOMAIN_VIOLATION", "OTHER")
    assert ts28111.perceived_severities() == ("INDETERMINATE", "CRITICAL", "MAJOR", "MINOR", "WARNING", "CLEARED")
    assert ts28111.trend_indications() == ("MORE_SEVERE", "NO_CHANGE", "LESS_SEVERE")
    assert ts28111.ack_states() == ("ACKNOWLEDGED", "UNACKNOWLEDGED")
    assert ts28111.notification_types() == ("notifyNewAlarm", "notifyChangedAlarmGeneral", "notifyAckStateChanged",
                                            "notifyCorrelatedNotificationChanged", "notifyComments", "notifyClearedAlarm",
                                            "notifyAlarmListRebuilt", "notifyPotentialFaultyAlarmList")


def test_the_probable_causes_and_the_alarm_record():
    """206 standardised probable causes (Annex B), no null among them, and the 30 members of AlarmRecord starting with objectInstance."""
    causes = ts28111.probable_causes()
    assert len(causes) == 206 and len(set(causes)) == 206 and None not in causes
    assert causes[:3] == ("INDETERMINATE", "ALARM_INDICATION_SIGNAL", "CALL_SETUP_FAILURE")
    fields = ts28111.alarm_record_fields()
    assert len(fields) == 30 and fields[:3] == ("objectInstance", "notificationId", "alarmRaisedTime")
    assert {"perceivedSeverity", "probableCause", "alarmType"} <= set(fields)


def test_is_probable_cause():
    """Any string or integer is a cause (vendor strings and integers are allowed); None, booleans and other types are not."""
    assert ts28111.is_probable_cause("CALL_SETUP_FAILURE") and ts28111.is_probable_cause("vendor-x") and ts28111.is_probable_cause(7)
    assert not ts28111.is_probable_cause(None) and not ts28111.is_probable_cause(True) and not ts28111.is_probable_cause(1.5)


def test_a_missing_enum_is_a_lookup_error():
    """A schema without an enum raises LookupError naming the file."""
    with pytest.raises(LookupError, match="TS28111_FaultNrm.yaml has no enum for AlarmRecord"):
        ts28111._enum("AlarmRecord")


# ---- TS 28.552 and TS 28.554, as data

def test_the_counters_are_the_golden_list():
    """The counters, their clauses, units and classes are exactly the snapshot (a change is reviewed here)."""
    assert [(c.name, c.clause, c.unit, c.measured_on) for c in dictionary.COUNTERS] == COUNTERS
    assert all(c.description for c in dictionary.COUNTERS)


def test_the_families_are_covered():
    """Every family the plan names is present, and the family is the name's first part."""
    sizes = {family: len(dictionary.counters(family)) for family in ("DRB", "RRU", "PEE", "MM", "RRC")}
    assert sizes == {"DRB": 8, "RRU": 6, "PEE": 10, "MM": 9, "RRC": 6}
    assert sum(sizes.values()) == len(dictionary.counters()) == 39
    assert dictionary.counter("MM.HoExeInterReq").family == "MM"


def test_the_kpis_are_the_golden_list_and_read_only_known_counters():
    """The KPIs are the snapshot, each has a description, and every counter a formula reads is in the dictionary."""
    assert [(k.name, k.clause, k.unit) for k in dictionary.KPIS] == KPIS
    for item in dictionary.KPIS:
        assert item.description
        assert dictionary.inputs(item.formula), item.name
        for name in dictionary.inputs(item.formula):
            dictionary.counter(name)


def test_lookup_of_an_unknown_name_is_a_key_error():
    """An unknown counter or KPI raises KeyError."""
    with pytest.raises(KeyError):
        dictionary.counter("DRB.Nothing")
    with pytest.raises(KeyError):
        dictionary.kpi("Nothing")


def test_inputs_are_in_order_of_first_use():
    """inputs lists each counter once, in order of first use, and ignores a bare word."""
    assert dictionary.inputs("A.b + C.d * A.b + 3") == ("A.b", "C.d")
    assert dictionary.inputs("x + 1") == ()


# ---- evaluation

def test_energy_efficiency_6_7_1():
    """(900 + 100) Mbit over 2 kWh is 1e9 bit / 7.2e6 J."""
    value = dictionary.evaluate("EE.DataVolume", {"DRB.PdcpSduVolumeDL": 900, "DRB.PdcpSduVolumeUL": 100, "PEE.Energy": 2})
    assert value == pytest.approx(1000 * 1_000_000 / (2 * 3_600_000))


def test_the_other_kpis_evaluate():
    """The success rates and the throughput evaluate over plain values."""
    assert dictionary.evaluate("InterGnbHoSuccessRate", {"MM.HoExeInterReq": 200, "MM.HoExeInterSucc": 150}) == 75.0
    assert dictionary.evaluate("DRBAccessibility", {"RRC.ConnEstabSucc.Cause": 9, "RRC.ConnEstabAtt.Cause": 10,
                                                    "DRB.EstabSucc.5QI": 5, "DRB.EstabAtt.5QI": 10}) == pytest.approx(45.0)
    assert dictionary.evaluate("DlUeThroughput", {"DRB.UEThpDl": 12.5}) == 12.5


# Table: formulas with their value over A.a=6, B.b=3.
@pytest.mark.parametrize(("formula", "value"), [
    ("A.a + B.b", 9.0), ("A.a - B.b", 3.0), ("A.a * B.b", 18.0), ("A.a / B.b", 2.0), ("-A.a", -6.0), ("+A.a", 6.0), ("2.5", 2.5),
    ("(A.a + 2) * B.b", 24.0), ("A.a - -B.b", 9.0),
])
def test_a_formula_is_evaluated(formula, value):
    """Arithmetic over counter names and numbers evaluates as written."""
    assert dictionary.evaluate(formula, {"A.a": 6, "B.b": 3}) == value


def test_a_missing_value_or_zero_divisor():
    """A counter without a value is a KeyError naming it; dividing by zero raises ZeroDivisionError."""
    with pytest.raises(KeyError, match="B.b"):
        dictionary.evaluate("A.a + B.b", {"A.a": 1})
    with pytest.raises(ZeroDivisionError):
        dictionary.evaluate("A.a / B.b", {"A.a": 1, "B.b": 0})


# Table: formulas outside the grammar, with the message each gets.
@pytest.mark.parametrize(("formula", "message"), [
    ("A.a +", "is not an arithmetic expression"),
    ("A.a ** 2", "uses Pow"),
    ("A.a // 2", "uses FloorDiv"),
    ("A.a % 2", "uses Mod"),
    ("abs(A.a)", "uses Call"),
    ("A.a if B.b else 1", "uses IfExp"),
    ("A.a < B.b", "uses Compare"),
    ("'x'", "constant that is not a number"),
    ("True", "constant that is not a number"),
    ("A.a + x", "names 'x', which is not a counter"),
    ("~A.a", "uses Invert"),
    ("A.a[0]", "uses Subscript"),
    ("(A.a, 1)", "uses Tuple"),
])
def test_a_formula_outside_the_grammar_is_refused(formula, message):
    """Only + - * /, unary signs, numbers and counter names are allowed; anything else is a ValueError before evaluation."""
    with pytest.raises(ValueError, match=message):
        dictionary.evaluate(formula, {"A.a": 1, "B.b": 1})


def test_every_kpi_is_inside_the_grammar():
    """No KPI formula is refused by the grammar."""
    for item in dictionary.KPIS:
        dictionary._compile(item.formula)
