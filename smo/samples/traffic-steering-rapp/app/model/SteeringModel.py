"""CongestionSteeringModel — the Traffic Steering rApp's model and its
pairwise steering planner (Wave 10.4, W10.4-02, decision D10.4-2).

Forecast. The next hour's congestion score of a cell is

    forecast = score + w0 + w1·(score − score an hour ago) + w2·profile[hour]

`profile[h]` is the learned mean change of the score from hour h to h+1, so
the morning ramp is anticipated.

Transfer. One steering step moves a fraction of the source's score to its
target(s):
  * CONNECTED, per dB of CIO on S → T: `transfer["CONNECTED"]` · score_S
    goes to T;
  * IDLE, per reselection-priority step from S towards a layer:
    `transfer["IDLE"]` · score_S, spread over S's neighbours on that layer.

Both fractions are learned from history in which the biases varied.

Planning, pairwise:
  * a cell forecast at ACT (70) or more takes one step towards the
    candidate whose forecast after the transfer is lowest, preferring idle
    over connected on a tie;
  * no target may end above TARGET_MAX (55);
  * between RELEASE (50) and ACT nothing changes (hysteresis);
  * below RELEASE, the cell's steering is released one step at a time.

The artifact is plain JSON in a .zip (`steering_model.json`) stored in
AIMgF's model registry. Like the other reference models, it is pure Python.
"""

import io
import json
import math
import zipfile
from dataclasses import asdict, dataclass, field

MODEL_TYPE = "CongestionSteeringModel"
ARTIFACT_MEMBER = "steering_model.json"
ACT, RELEASE, TARGET_MAX = 70.0, 50.0, 55.0
CIO_STEP_DB, PRIO_STEP = 2, 1


@dataclass
class SteeringModel:
    """The trained model: the forecast weights, the hour-of-day profile, the learned transfer per CIO dB and per priority step, the fit (RMSE,
    sample count), the thresholds and the version. The methods forecast a cell's score, say how much one step moves, and plan the steering of a
    cluster pairwise; the artifact is this dataclass as JSON.
    """
    weights: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # [drift, trend gain, profile gain]
    profile: list[float] = field(default_factory=lambda: [0.0] * 24)
    transfer: dict = field(default_factory=lambda: {"CONNECTED": 0.0, "IDLE": 0.0})
    rmse: float = 0.0
    act: float = ACT
    release: float = RELEASE
    target_max: float = TARGET_MAX
    trained_samples: int = 0
    model_type: str = MODEL_TYPE
    version: str = "1.0.0"

    def forecast(self, score: float, score_hour_ago: float | None, hour: int) -> float:
        """Next-hour congestion score (0 to 100): the current score plus the drift, the gain on the last hour's trend and the gain on the hour's
        learned profile. Without a score from an hour ago the trend is 0.
        """
        trend = score - (score_hour_ago if score_hour_ago is not None else score)
        w0, w1, w2 = self.weights
        return round(max(0.0, min(100.0, score + w0 + w1 * trend + w2 * self.profile[hour % 24])), 3)

    def moved(self, knob: str, source_score: float) -> float:
        """Score points one step moves out of the source."""
        step = CIO_STEP_DB if knob == "CONNECTED" else PRIO_STEP
        return round(self.transfer[knob] * step * source_score, 3)

    def band(self, forecast: float) -> str:
        return "CONGESTED" if forecast >= self.act else "HOLD" if forecast >= self.release else "NORMAL"

    def confidence(self) -> float:
        """1 for a perfect fit, falling as the forecast RMSE approaches the 20-point hysteresis band."""
        return round(max(0.0, 1.0 - self.rmse / (self.act - self.release)), 3)

    # ------------------------------------------------------------ pairwise planner

    def plan(self, state: dict, candidates: dict[str, list[dict]], releases: dict[str, list[dict]]) -> dict[str, dict]:
        """Per source cell, one decision.

        `state` is {cell: {"score", "forecast"}}. `candidates` is
        {source: [{"knob": "CONNECTED", "target": T} | {"knob": "IDLE",
        "layer": L, "targets": [T, ...]}]}, and lists the moves the guards
        and bounds leave open for a source that may act. `releases` is
        {source: [{"knob", "target" | "layer", ...}]}, the steering in force
        that may be stepped back, in the order to release it."""
        running = {c: s["forecast"] for c, s in state.items()}
        out = {}
        for source in sorted(state, key=lambda c: (-state[c]["forecast"], c)):
            f = running[source]
            if f >= self.act and source in candidates:
                best, rejected = None, []
                for cand in candidates[source]:
                    moved = self.moved(cand["knob"], state[source]["score"])
                    targets = [cand["target"]] if cand["knob"] == "CONNECTED" else cand["targets"]
                    share = moved / len(targets)
                    after = max(running.get(t, 0.0) + share for t in targets)
                    if after > self.target_max:
                        rejected.append({**cand, "targetForecastAfter": round(after, 3), "reason": "TARGET_CAPACITY"})
                        continue
                    key = (after, 0 if cand["knob"] == "IDLE" else 1, ",".join(targets))
                    if best is None or key < best[0]:
                        best = (key, cand, moved, share, targets, after)
                if best:
                    _, cand, moved, share, targets, after = best
                    running[source] = round(f - moved, 3)
                    for t in targets:
                        running[t] = round(running.get(t, 0.0) + share, 3)
                    out[source] = {"decision": f"STEER_{cand['knob']}", "reason": f"CONGESTED:{f:.1f}", "move": cand,
                                   "movedScore": moved, "targetForecastAfter": round(after, 3),
                                   "sourceForecastAfter": running[source], "rejected": rejected}
                else:
                    out[source] = {"decision": "NO_CHANGE", "reason": "NO_ELIGIBLE_TARGET", "rejected": rejected}
            elif f < self.release and releases.get(source):
                out[source] = {"decision": f"RELEASE_{releases[source][0]['knob']}", "reason": "LOAD_RELIEVED",
                               "move": releases[source][0]}
            else:
                out[source] = {"decision": "NO_CHANGE", "reason": "HOLD_ZONE" if f >= self.release else "NOT_CONGESTED"}
        return out

    # ------------------------------------------------------------ artifact

    def to_artifact(self) -> bytes:
        """Serialises the model as a .zip holding one JSON member (`steering_model.json`), the form stored in AIMgF's model registry."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(ARTIFACT_MEMBER, json.dumps(asdict(self), indent=1, sort_keys=True))
        return buf.getvalue()

    @classmethod
    def from_artifact(cls, data: bytes) -> "SteeringModel":
        """Reads a model back from the bytes `to_artifact` made; raises zipfile.BadZipFile or KeyError for anything else."""
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return cls.from_dict(json.loads(z.read(ARTIFACT_MEMBER)))

    @classmethod
    def from_dict(cls, d: dict) -> "SteeringModel":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


def solve_least_squares(rows: list[list[float]], targets: list[float]) -> list[float]:
    """Normal equations by Gaussian elimination with partial pivoting (tiny ridge)."""
    n = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) + (1e-6 if i == j else 0.0) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * t for r, t in zip(rows, targets)) for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot], b[col], b[pivot] = a[pivot], a[col], b[pivot], b[col]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            a[r] = [x - f * y for x, y in zip(a[r], a[col])]
            b[r] -= f * b[col]
    w = [0.0] * n
    for r in range(n - 1, -1, -1):
        w[r] = (b[r] - sum(a[r][c] * w[c] for c in range(r + 1, n))) / a[r][r]
    return w


def rmse(errors: list[float]) -> float:
    return math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else 0.0
