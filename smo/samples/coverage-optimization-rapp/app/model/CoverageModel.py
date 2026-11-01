"""CoverageSensitivityModel — the Coverage Optimization rApp's model and its
joint neighbour optimiser (Wave 10.3, W10.3-02, decision D10.3-2).

Each cell has three problem shares, in % of its measurement reports:
WEAK_COVERAGE, OVERSHOOT and PILOT_POLLUTION. A cell's "reach" grows when
it is uptilted or its power is raised. The model is linear in the reach
steps:

    Δshare[k]_i = own_t[k]·t_i + own_p[k]·p_i
                  + Σ_j c_ij · (nbr_t[k]·t_j + nbr_p[k]·p_j)

  * t is the uptilt in degrees (−Δtilt), and p the power step in dB.
  * c_ij is neighbour j's overlap with cell i, in tens of percent.

The 12 sensitivities (3 shares × 4 terms) are learned by least squares from
history in which tilt and power varied.

The optimiser searches the cluster jointly. Each cell can make one move:
NONE, DOWNTILT, UPTILT (1°), POWER_UP or POWER_DOWN (1 dB). At most
MAX_CELLS_PER_PASS cells move. Each candidate move set is scored by:

    objective = Σ_cells Σ_k max(0, share − 5 %)  +  CELL_COST · cells moved

The best move set is taken if it beats doing nothing by at least MIN_GAIN,
and no cell's own excess is predicted to grow by more than
WORSEN_TOLERANCE. Because neighbours' terms are in the model, pollution in
one cell can be cured by downtilting the neighbour that overshoots into it.

The artifact is plain JSON in a .zip (`coverage_model.json`) stored in
AIMgF's model registry. Like the other reference models, it is pure Python.
"""

import io
import itertools
import json
import math
import zipfile
from dataclasses import asdict, dataclass, field

MODEL_TYPE = "CoverageSensitivityModel"
ARTIFACT_MEMBER = "coverage_model.json"
PROBLEM_KEYS = ("WEAK_COVERAGE", "OVERSHOOT", "PILOT_POLLUTION")
TERMS = ("ownTilt", "ownPower", "neighbourTilt", "neighbourPower")
THRESHOLD = 5.0          # % of reports, per problem class
OVERLAP_SCALE = 10.0     # overlap % → coupling weight
MAX_CELLS_PER_PASS = 2
CELL_COST = 0.5
MIN_GAIN = 0.75
WORSEN_TOLERANCE = 0.5
# move → (Δtilt in degrees, positive = downtilt; Δpower in dB)
MOVES = {"NONE": (0, 0), "DOWNTILT": (1, 0), "UPTILT": (-1, 0), "POWER_UP": (0, 1), "POWER_DOWN": (0, -1)}


def reach(move: str) -> tuple[float, float]:
    """(uptilt t, power step p) of a move."""
    d_tilt, d_power = MOVES[move]
    return -float(d_tilt), float(d_power)


def excess(shares: dict[str, float]) -> float:
    return sum(max(0.0, shares[k] - THRESHOLD) for k in PROBLEM_KEYS)


def dominant_problem(shares: dict[str, float]) -> str | None:
    """The problem class with the largest share, or None when even that is not above the 5 % threshold (the cell is healthy)."""
    worst = max(PROBLEM_KEYS, key=lambda k: shares[k])
    return worst if shares[worst] > THRESHOLD else None


@dataclass
class CoverageModel:
    # {problem: [ownTilt, ownPower, neighbourTilt, neighbourPower]}; untrained = no effect
    """The trained model: 12 sensitivities, their fit (RMSE, sample count) and the version. The methods predict the shares after a move set and
    search the best move set for a cluster; the artifact is this dataclass as JSON.
    """
    sensitivities: dict = field(default_factory=lambda: {k: [0.0, 0.0, 0.0, 0.0] for k in PROBLEM_KEYS})
    rmse: float = 0.0
    threshold: float = THRESHOLD
    trained_samples: int = 0
    model_type: str = MODEL_TYPE
    version: str = "1.0.0"

    # ------------------------------------------------------------ prediction

    def delta(self, own: tuple[float, float], neighbour_reach: tuple[float, float]) -> dict[str, float]:
        """Predicted share changes for a cell, given its own (t, p) and the
        overlap-weighted sum of its neighbours' (t, p)."""
        t, p = own
        nt, np_ = neighbour_reach
        return {k: s[0] * t + s[1] * p + s[2] * nt + s[3] * np_ for k, s in self.sensitivities.items()}

    def predict(self, state: dict, plan: dict[str, str]) -> dict[str, dict[str, float]]:
        """{cell: predicted shares} if `plan` ({cell: move}) were applied.
        `state` is {cell: {"shares": {...}, "overlaps": {neighbour: %}}}."""
        out = {}
        for cell, s in state.items():
            own = reach(plan.get(cell, "NONE"))
            nt = np_ = 0.0
            for nbr, ov in s.get("overlaps", {}).items():
                if nbr in plan:
                    t, p = reach(plan[nbr])
                    nt += ov / OVERLAP_SCALE * t
                    np_ += ov / OVERLAP_SCALE * p
            d = self.delta(own, (nt, np_))
            out[cell] = {k: round(min(100.0, max(0.0, s["shares"][k] + d[k])), 3) for k in PROBLEM_KEYS}
        return out

    def objective(self, shares_by_cell: dict[str, dict[str, float]]) -> float:
        return round(sum(excess(s) for s in shares_by_cell.values()), 3)

    # ------------------------------------------------------------ joint optimiser

    def optimise(self, state: dict, allowed: dict[str, list[str]], max_cells: int = MAX_CELLS_PER_PASS) -> dict:
        """The best move set for the cluster. `allowed` is {cell: moves the
        guards and bounds leave open}; a cell missing from it can't move."""
        current = {c: s["shares"] for c, s in state.items()}
        before = self.objective(current)
        best_plan, best_score, best_pred = {}, before, current
        movable = sorted(c for c in state if allowed.get(c))
        for n in range(1, max_cells + 1):
            for cells in itertools.combinations(movable, n):
                for moves in itertools.product(*(sorted(m for m in allowed[c] if m != "NONE") for c in cells)):
                    plan = dict(zip(cells, moves))
                    pred = self.predict(state, plan)
                    if any(excess(pred[c]) > excess(current[c]) + WORSEN_TOLERANCE for c in state):
                        continue
                    score = self.objective(pred) + CELL_COST * n
                    if score < best_score - 1e-9:
                        best_plan, best_score, best_pred = plan, score, pred
        gain = round(before - best_score, 3)
        if not best_plan or gain < MIN_GAIN:
            best_plan, best_pred, gain = {}, current, 0.0
        return {"plan": best_plan, "objectiveBefore": before, "objectiveAfter": self.objective(best_pred),
                "gain": gain, "predicted": best_pred,
                "drivers": {c: self.driver(state, c, m) for c, m in best_plan.items()}}

    def driver(self, state: dict, cell: str, move: str) -> str:
        """The problem a move does most for, as "<PROBLEM>@<cell>"."""
        current = {c: s["shares"] for c, s in state.items()}
        pred = self.predict(state, {cell: move})
        best, best_cut = f"{dominant_problem(current[cell]) or 'COVERAGE'}@{cell}", 0.0
        for c in sorted(state):
            for k in PROBLEM_KEYS:
                cut = max(0.0, current[c][k] - THRESHOLD) - max(0.0, pred[c][k] - THRESHOLD)
                if cut > best_cut + 1e-9:
                    best, best_cut = f"{k}@{c}", cut
        return best

    def confidence(self) -> float:
        """1 for a perfect fit, falling as the RMSE approaches 2 points."""
        return round(max(0.0, 1.0 - self.rmse / 2.0), 3)

    # ------------------------------------------------------------ artifact

    def to_artifact(self) -> bytes:
        """Serialises the model as a .zip holding one JSON member (`coverage_model.json`), the form stored in AIMgF's model registry."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(ARTIFACT_MEMBER, json.dumps(asdict(self), indent=1, sort_keys=True))
        return buf.getvalue()

    @classmethod
    def from_artifact(cls, data: bytes) -> "CoverageModel":
        """Reads a model back from the bytes `to_artifact` made; raises zipfile.BadZipFile or KeyError for anything else."""
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return cls.from_dict(json.loads(z.read(ARTIFACT_MEMBER)))

    @classmethod
    def from_dict(cls, d: dict) -> "CoverageModel":
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
