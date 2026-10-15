"""The Traffic Steering rApp's decision engine (Wave 10.4, W10.4-05/07,
decisions D10.4-1 / D10.4-4). Pure functions, no I/O.

Per source cell, in order:

  1. KPI-verified revert. A cell that just steered is OBSERVING until 60
     minutes of post-change PM exist. The change is REVERTED if any of these
     hold; otherwise it is CONFIRMED:
       - a target became congested (score 70 or more);
       - the source ended worse than its own forecast without steering (by
         more than 2 points);
       - for a CIO change, the relation's handover failure rate rose by more
         than 2 points.
  2. Source guards. Any one of these holds the cell:
       - an EMERGENCY or incident-zone cell, or an active critical alarm on
         the cell (or on the managed element as a whole);
       - the cell is asleep (O1 or EnergySaving SLEEP / PRE_SLEEP);
       - it is in a Coverage change set under observation;
       - fewer than 10 PM samples in the window;
       - the cell changed less than 60 minutes ago (pacing).
  3. Target exclusions. A neighbour is not a target if any of these hold:
       - it is protected, asleep, pre-sleep or less than 30 minutes past a
         wake, or a critical alarm holds it;
       - it is in a Coverage change set under observation;
       - it steered load to this cell in the last 6 hours (anti-oscillation).
  4. Knob choice and bounds:
       - an inter-frequency target is steered in idle mode
         (NRFreqRelation.cellReselectionPriority, + 1, within baseline ± 2
         and 0–7);
       - connected CIO (+ 2 dB on NRCellRelation, within the shared baseline
         ± 6 envelope) is used for an intra-frequency target, or once the
         idle knob is at its bound;
       - CIO is never biased on a relation with isMLBAllowed or isHOAllowed
         false, nor on one the Mobility rApp is observing.
  5. The model's pairwise planner picks the target, and keeps every target
     at or below 55 after the transfer.
"""

import datetime
from dataclasses import dataclass, field

from .model.SteeringModel import ACT, CIO_STEP_DB, PRIO_STEP

PACING = datetime.timedelta(minutes=60)
OBSERVATION = datetime.timedelta(minutes=60)
AFTER_WAKE = datetime.timedelta(minutes=30)
ANTI_OSCILLATION = datetime.timedelta(hours=6)
MIN_SAMPLES = 10
CIO_ENVELOPE_DB = 6          # shared with the Mobility rApp (the DMRO bounds)
PRIO_DEVIATION, PRIO_MIN, PRIO_MAX = 2, 0, 7
SOURCE_WORSE_POINTS, HO_FAIL_POINTS = 2.0, 2.0

STEADY, OBSERVING = "STEADY", "OBSERVING"
NO_CHANGE, REVERT = "NO_CHANGE", "REVERT"


@dataclass
class Neighbour:
    """A neighbour of the source cell as a steering target: its layer, the facts that exclude it (protected, asleep, recently woken, in a Coverage
    change set, critical alarm), and the state of the relation source to it (CIO, whether handover and load balancing are allowed, whether the
    Mobility rApp is observing it).
    """
    cell: str
    layer: str
    protected: bool = False
    asleep: bool = False
    last_woken: datetime.datetime | None = None
    coverage_observing: bool = False
    critical_alarm: bool = False  # W10-alarm-cellref: a critical alarm raised on this cell
    held: bool = False            # call flow 25e: its own step waits for the operator or a revert / rollback of it for a person
    # the relation source → this neighbour
    cio: int = 0
    ho_allowed: bool = True
    mlb_allowed: bool = True
    mro_observing: bool = False


@dataclass
class SourceInput:
    """One source cell's inputs to a pass: its layer, PM sample count, the baselines, its neighbours, the live idle priorities, the steering this
    rApp already has in force, and the guard facts (class and incident zone, alarm, sleep, Coverage observation, last change, when each
    neighbour last steered load to it).
    """
    cell: str
    layer: str
    samples: float
    baseline_cio: int
    baseline_priority: int
    neighbours: list[Neighbour] = field(default_factory=list)
    priorities: dict[str, int] = field(default_factory=dict)     # live priority towards each other layer
    steering: dict = field(default_factory=lambda: {"cio": {}, "prio": {}})   # this rApp's own steering in force
    guard: dict = field(default_factory=dict)
    critical_alarm: bool = False
    asleep: bool = False
    coverage_observing: bool = False
    last_changed_at: datetime.datetime | None = None
    steered_to_me: dict[str, datetime.datetime] = field(default_factory=dict)  # neighbour → when it last steered to this cell


def _protected(guard: dict) -> bool:
    return guard.get("cellClass") == "EMERGENCY" or bool(guard.get("incidentZone"))


def source_guards(s: SourceInput, now: datetime.datetime) -> dict:
    """Runs the guards on a source cell and returns {"passed": bool, "blocks": [{guard, level, detail?}]}.

    Each guard that fires adds a block, so the audit trail lists every reason and not only the first. `now` is the newest window's time, not the
    wall clock, so replaying old data gives the same answer. No I/O.
    """
    blocks = []
    if _protected(s.guard):
        blocks.append({"guard": "PROTECTED_CELL", "level": "HARD"})
    if s.critical_alarm:
        blocks.append({"guard": "CRITICAL_ALARM", "level": "HARD"})
    if s.asleep:
        blocks.append({"guard": "CELL_ASLEEP", "level": "HARD"})
    if s.coverage_observing:
        blocks.append({"guard": "COVERAGE_OBSERVING", "level": "MEDIUM"})
    if s.samples < MIN_SAMPLES:
        blocks.append({"guard": "INSUFFICIENT_SAMPLES", "level": "SOFT", "detail": f"{s.samples:.0f} samples"})
    if s.last_changed_at and now - s.last_changed_at < PACING:
        blocks.append({"guard": "PACING", "level": "SOFT", "detail": f"changed at {s.last_changed_at.isoformat()}"})
    return {"passed": not blocks, "blocks": blocks}


def target_exclusion(s: SourceInput, n: Neighbour, now: datetime.datetime) -> str | None:
    """Returns the reason code (TARGET_PROTECTED, TARGET_CRITICAL_ALARM, TARGET_AWAITING_APPROVAL, TARGET_ASLEEP, TARGET_RECENTLY_WOKEN,
    TARGET_COVERAGE_OBSERVING or ANTI_OSCILLATION) that rules a neighbour out as a steering target, or None when it is eligible. The first reason
    that applies is returned.
    """
    if n.protected:
        return "TARGET_PROTECTED"
    if n.critical_alarm:
        return "TARGET_CRITICAL_ALARM"
    if n.held:
        return "TARGET_AWAITING_APPROVAL"
    if n.asleep:
        return "TARGET_ASLEEP"
    if n.last_woken and now - n.last_woken < AFTER_WAKE:
        return "TARGET_RECENTLY_WOKEN"
    if n.coverage_observing:
        return "TARGET_COVERAGE_OBSERVING"
    back = s.steered_to_me.get(n.cell)
    if back and now - back < ANTI_OSCILLATION:
        return "ANTI_OSCILLATION"
    return None


def options(s: SourceInput, now: datetime.datetime) -> tuple[list[dict], list[dict], list[dict]]:
    """(steering candidates, releases, exclusions) for a source cell."""
    excluded, eligible = [], []
    for n in s.neighbours:
        why = target_exclusion(s, n, now)
        (excluded.append({"target": n.cell, "reason": why}) if why else eligible.append(n))
    candidates, idle_layers = [], set()
    for layer in sorted({n.layer for n in s.neighbours if n.layer != s.layer}):
        on_layer = [n for n in s.neighbours if n.layer == layer]
        if any(n not in eligible for n in on_layer):
            continue
        prio = s.priorities.get(layer, s.baseline_priority)
        if prio + PRIO_STEP > min(PRIO_MAX, s.baseline_priority + PRIO_DEVIATION):
            excluded.append({"layer": layer, "reason": "IDLE_AT_BOUND"})
            continue
        idle_layers.add(layer)
        candidates.append({"knob": "IDLE", "layer": layer, "targets": [n.cell for n in on_layer],
                           "ref": f"NRFreqRelation={s.cell}-{layer}", "from": prio, "to": prio + PRIO_STEP})
    for n in eligible:
        if n.layer in idle_layers:
            continue                                           # idle first towards another layer
        why = ("MLB_NOT_ALLOWED" if not (n.mlb_allowed and n.ho_allowed) else "MRO_OBSERVING" if n.mro_observing
               else "CIO_AT_BOUND" if n.cio + CIO_STEP_DB > s.baseline_cio + CIO_ENVELOPE_DB else None)
        if why:
            excluded.append({"target": n.cell, "knob": "CONNECTED", "reason": why})
            continue
        candidates.append({"knob": "CONNECTED", "target": n.cell, "ref": f"NRCellRelation={s.cell}-{n.cell}",
                           "from": n.cio, "to": n.cio + CIO_STEP_DB})
    releases = []
    live_cio = {n.cell: n.cio for n in s.neighbours}
    for target, bias in sorted((s.steering.get("cio") or {}).items(), key=lambda x: -x[1]):
        if bias > 0:
            cur = live_cio.get(target, s.baseline_cio + bias)
            releases.append({"knob": "CONNECTED", "target": target, "ref": f"NRCellRelation={s.cell}-{target}",
                             "from": cur, "to": cur - min(CIO_STEP_DB, bias)})
    for layer, steps in sorted((s.steering.get("prio") or {}).items()):
        if steps > 0:
            cur = s.priorities.get(layer, s.baseline_priority + steps)
            releases.append({"knob": "IDLE", "layer": layer, "ref": f"NRFreqRelation={s.cell}-{layer}",
                             "from": cur, "to": cur - PRIO_STEP})
    return candidates, releases, excluded


def kpi_check(change: dict, scores: dict[str, float], ho_fail: float | None, now: datetime.datetime) -> dict | None:
    """None while the change is still being observed; else the verdict."""
    changed_at = change["at"]
    if now <= changed_at or now - changed_at < OBSERVATION:
        return None
    source = change["source"]
    post_targets = {t: scores.get(t) for t in change["targets"]}
    causes = []
    if any(v is not None and v >= ACT for v in post_targets.values()):
        causes.append("TARGET_CONGESTED")
    if scores.get(source) is not None and scores[source] > change["preForecast"] + SOURCE_WORSE_POINTS:
        causes.append("SOURCE_WORSE")
    if change["knob"] == "CONNECTED" and ho_fail is not None and change.get("preHoFail") is not None \
            and ho_fail > change["preHoFail"] + HO_FAIL_POINTS:
        causes.append("HO_FAILURES")
    return {"preSource": change["preScore"], "preForecast": change["preForecast"], "postSource": scores.get(source),
            "preTargets": change["preTargets"], "postTargets": post_targets, "preHoFail": change.get("preHoFail"),
            "postHoFail": ho_fail, "verdict": "DEGRADED" if causes else "IMPROVED_OR_EQUAL", "causes": causes}
