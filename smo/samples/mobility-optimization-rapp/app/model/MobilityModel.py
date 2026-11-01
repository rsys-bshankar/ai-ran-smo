"""MobilityRobustnessPredictor — the Mobility Optimization rApp's model
(Wave 10.2, W10.2-02, decision D10.2-2). It combines a failure
classification with a persistence-anchored regression of the next-hour
mobility problem rate:

    futureRate = rateNow + w0 + w1 * (rateNow - rateHourAgo)

The rate is failures (too-late + too-early + wrong-cell) plus ping-pongs,
per handover attempt, in %.

The recommendation applies the MRO thresholds:
    ACT       predicted rate at least 5 %. The step direction comes from the
              dominant failure class: TOO_LATE raises CIO; TOO_EARLY and
              PING_PONG lower it; WRONG_CELL lowers it by 1 dB.
    HOLD      2–5 %, a hysteresis zone
    HEALTHY   below 2 %

The artifact is plain JSON in a .zip (`mobility_model.json`) stored in
AIMgF's model registry. Like the EnergySaving model, it is pure Python.
"""

import io
import json
import math
import zipfile
from dataclasses import asdict, dataclass, field

MODEL_TYPE = "MobilityRobustnessPredictor"
ARTIFACT_MEMBER = "mobility_model.json"
ACT_THRESHOLD = 5.0
HEALTHY_THRESHOLD = 2.0
STEP_DB = {"TOO_LATE": 2, "TOO_EARLY": -2, "PING_PONG": -2, "WRONG_CELL": -1}


@dataclass
class MobilityModel:
    """The trained predictor: the two regression weights (drift and trend gain), the fit (RMSE, sample count), the act and healthy thresholds and
    the version. The methods predict the next-hour problem rate and turn it into a RAISE_CIO, LOWER_CIO, HOLD or HEALTHY recommendation; the
    artifact is this dataclass as JSON.
    """
    weights: list[float] = field(default_factory=lambda: [0.0, 0.0])  # [drift, trend gain]; untrained = persistence
    rmse: float = 0.0
    act_threshold: float = ACT_THRESHOLD
    healthy_threshold: float = HEALTHY_THRESHOLD
    trained_samples: int = 0
    model_type: str = MODEL_TYPE
    version: str = "1.0.0"

    def predict(self, rate_now: float, rate_hour_ago: float | None = None) -> float:
        """Predicted next-hour problem rate (%), kept within 0 to 100: the current rate plus the drift and the gain on the last hour's trend. Without a
        window from an hour ago the trend is 0.
        """
        trend = rate_now - (rate_hour_ago if rate_hour_ago is not None else rate_now)
        w0, w1 = self.weights
        return max(0.0, min(100.0, rate_now + w0 + w1 * trend))

    def recommend(self, future_rate: float, cause: str | None) -> str:
        """RAISE_CIO or LOWER_CIO (by the dominant failure class) when the predicted rate is at least the act threshold and a cause is known; otherwise
        HOLD at or above the healthy threshold and HEALTHY below it.
        """
        if future_rate >= self.act_threshold and cause:
            return "RAISE_CIO" if STEP_DB[cause] > 0 else "LOWER_CIO"
        return "HOLD" if future_rate >= self.healthy_threshold else "HEALTHY"

    def confidence(self) -> float:
        """1 for a perfect fit, falling as the training RMSE approaches the
        3-point band between the healthy and act thresholds."""
        return round(max(0.0, 1.0 - self.rmse / (self.act_threshold - self.healthy_threshold)), 3)

    def infer(self, rate_now: float, rate_hour_ago: float | None, cause: str | None) -> dict:
        """The inference answer for one relation: {futureRate, cause, recommendation, confidence}."""
        future = self.predict(rate_now, rate_hour_ago)
        return {"futureRate": round(future, 3), "cause": cause, "recommendation": self.recommend(future, cause),
                "confidence": self.confidence()}

    def to_artifact(self) -> bytes:
        """Serialises the model as a .zip holding one JSON member (`mobility_model.json`), the form stored in AIMgF's model registry."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(ARTIFACT_MEMBER, json.dumps(asdict(self), indent=1, sort_keys=True))
        return buf.getvalue()

    @classmethod
    def from_artifact(cls, data: bytes) -> "MobilityModel":
        """Reads a model back from the bytes `to_artifact` made; raises zipfile.BadZipFile or KeyError for anything else."""
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return cls.from_dict(json.loads(z.read(ARTIFACT_MEMBER)))

    @classmethod
    def from_dict(cls, d: dict) -> "MobilityModel":
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
