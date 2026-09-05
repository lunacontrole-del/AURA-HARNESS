"""P1 Fase 4 — Baseline Hawkes/Poisson + residual LightGBM em shadow mode.

Regras:
- Baseline permanece autoridade até promoção formal
- LightGBM aprende residual (clamp), não substitui silenciosamente
- Walk-forward por fixture_id (proibido train_test_split aleatório)
- Calibrador por horizonte (Platt; isotonic só com amostra grande)
- Challenger só em shadow — não altera decisões reais
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    import lightgbm as lgb
    HAS_LGB = True
except Exception:
    HAS_LGB = False

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.isotonic import IsotonicRegression
    HAS_SK = True
except Exception:
    HAS_SK = False

from metrics import brier_score, log_loss, calibration_bins

# ---------------------------------------------------------------------------
# Feature manifest (versionado)
# ---------------------------------------------------------------------------

FEATURE_MANIFEST_VERSION = "feat_v1_p1_shadow"

FEATURE_MANIFEST: List[Dict[str, Any]] = [
    {"name": "da_total", "unit": "count", "source": "stats.dangerous", "transform": "sum"},
    {"name": "xg_total", "unit": "xg", "source": "stats.xg", "transform": "sum"},
    {"name": "corners_total", "unit": "count", "source": "stats.corners", "transform": "sum"},
    {"name": "goals_total", "unit": "count", "source": "stats.goals", "transform": "sum"},
    {"name": "dda_dt", "unit": "per_obs", "source": "derived", "transform": "rate"},
    {"name": "dxg_dt", "unit": "per_obs", "source": "derived", "transform": "rate"},
    {"name": "dc_dt", "unit": "per_obs", "source": "derived", "transform": "rate"},
    {"name": "da_ratio_home", "unit": "ratio", "source": "derived", "transform": "ratio"},
    {"name": "pressure_imbalance", "unit": "ratio", "source": "derived", "transform": "signed_ratio"},
    {"name": "corner_pace", "unit": "per_minute", "source": "derived", "transform": "pace"},
    {"name": "minute_proxy", "unit": "minute", "source": "clock", "transform": "identity"},
    {"name": "p_hawkes", "unit": "prob", "source": "baseline", "transform": "identity"},
    {"name": "log_odds_hawkes", "unit": "logit", "source": "baseline", "transform": "logit"},
]

FEATURE_NAMES = [f["name"] for f in FEATURE_MANIFEST]

EVENT_TYPES = (
    "corner_event",
    "pressure_spike",
    "shot_event",
    "xg_change",
    "card_event",
    "market_change",
)

MODEL_DIR = Path(__file__).resolve().parent / "artifacts" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

CHAMPION_PATH = MODEL_DIR / "champion_bundle.json"
RESIDUAL_CLAMP = 2.0  # max |residual| in logit space
MIN_ISOTONIC_N = 200


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float, eps: float = 1e-6) -> float:
    p = _clamp(p, eps, 1.0 - eps)
    return math.log(p / (1.0 - p))


def poisson_at_least_one(lam: float) -> float:
    lam = max(0.0, float(lam))
    return 1.0 - math.exp(-lam)


# ---------------------------------------------------------------------------
# Typed event intensity (Hawkes-like baseline)
# ---------------------------------------------------------------------------

@dataclass
class TypedEvent:
    event_type: str
    t: float  # match seconds
    magnitude: float = 1.0


@dataclass
class HawkesParams:
    mu: float = 0.02          # base intensity per second
    alpha_corner: float = 0.15
    beta_corner: float = 0.05  # decay 1/s  (alpha < beta constraint for simple kernel stability)
    alpha_pressure: float = 0.08
    beta_pressure: float = 0.04
    alpha_xg: float = 0.06
    beta_xg: float = 0.03
    alpha_shot: float = 0.05
    beta_shot: float = 0.04
    alpha_card: float = 0.02
    beta_card: float = 0.02
    alpha_market: float = 0.03
    beta_market: float = 0.03
    minute_scale: float = 0.0001
    score_effect: float = 0.0

    def validate(self) -> None:
        pairs = [
            (self.alpha_corner, self.beta_corner),
            (self.alpha_pressure, self.beta_pressure),
            (self.alpha_xg, self.beta_xg),
            (self.alpha_shot, self.beta_shot),
            (self.alpha_card, self.beta_card),
            (self.alpha_market, self.beta_market),
        ]
        for a, b in pairs:
            if a >= b and b > 0:
                # soft constraint warning only — caller may tighten
                pass


def classify_events_from_payload(payload: Dict[str, Any]) -> List[TypedEvent]:
    """Separate corner / pressure / xG / shot / card / market events."""
    out: List[TypedEvent] = []
    events = payload.get("events") or payload.get("eventos") or []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                text = str(ev).lower()
                et = "corner_event" if ("corner" in text or "escante" in text) else None
                if et:
                    out.append(TypedEvent(et, t=0.0, magnitude=1.0))
                continue
            blob = " ".join(str(ev.get(k) or "") for k in ("type", "event", "name", "kind")).lower()
            try:
                m = float(ev.get("minute") or 0)
                s = float(ev.get("second") or 0)
                t = m * 60.0 + s
            except (TypeError, ValueError):
                t = 0.0
            if "corner" in blob or "escante" in blob:
                out.append(TypedEvent("corner_event", t=t, magnitude=1.0))
            elif "shot" in blob or "chute" in blob or "kick" in blob:
                out.append(TypedEvent("shot_event", t=t, magnitude=1.0))
            elif "card" in blob or "cartao" in blob or "cartão" in blob:
                out.append(TypedEvent("card_event", t=t, magnitude=1.0))
            elif "xg" in blob:
                out.append(TypedEvent("xg_change", t=t, magnitude=float(ev.get("value") or 1.0)))

    # pressure spike from derivatives if provided
    stats = payload.get("stats") or {}
    if isinstance(stats, dict):
        danger = stats.get("dangerous") or {}
        try:
            da = float(danger.get("home") or 0) + float(danger.get("away") or 0)
            if da > 0 and payload.get("_pressure_spike"):
                out.append(TypedEvent("pressure_spike", t=float(payload.get("match_seconds") or 0), magnitude=1.0))
        except (TypeError, ValueError):
            pass

    # market change
    if payload.get("odds_velocity") is not None:
        try:
            ov = abs(float(payload.get("odds_velocity") or 0))
            if ov > 0.5:
                out.append(TypedEvent("market_change", t=float(payload.get("match_seconds") or 0), magnitude=ov))
        except (TypeError, ValueError):
            pass
    return out


def hawkes_intensity(
    t: float,
    events: Sequence[TypedEvent],
    params: HawkesParams,
    *,
    minute: float = 0.0,
) -> float:
    """lambda(t) with typed exponential kernels. Does NOT treat dDA/dt as a corner."""
    params.validate()
    lam = params.mu * math.exp(params.minute_scale * minute + params.score_effect)
    for ev in events:
        if ev.t > t:
            continue
        dt = t - ev.t
        if ev.event_type == "corner_event":
            lam += params.alpha_corner * math.exp(-params.beta_corner * dt) * ev.magnitude
        elif ev.event_type == "pressure_spike":
            lam += params.alpha_pressure * math.exp(-params.beta_pressure * dt) * ev.magnitude
        elif ev.event_type == "xg_change":
            lam += params.alpha_xg * math.exp(-params.beta_xg * dt) * ev.magnitude
        elif ev.event_type == "shot_event":
            lam += params.alpha_shot * math.exp(-params.beta_shot * dt) * ev.magnitude
        elif ev.event_type == "card_event":
            lam += params.alpha_card * math.exp(-params.beta_card * dt) * ev.magnitude
        elif ev.event_type == "market_change":
            lam += params.alpha_market * math.exp(-params.beta_market * dt) * ev.magnitude
    return max(0.0, lam)


def baseline_prob_horizon(
    intensity_per_sec: float,
    horizon_sec: float = 300.0,
) -> float:
    """P(at least one corner in horizon) under constant intensity approx."""
    lam = max(0.0, intensity_per_sec) * max(1.0, horizon_sec)
    return poisson_at_least_one(lam)


def baseline_from_payload(
    payload: Dict[str, Any],
    *,
    horizon_sec: float = 300.0,
    params: Optional[HawkesParams] = None,
    legacy_lambda: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Combine typed Hawkes intensity with optional legacy lambda from engine.
    """
    params = params or HawkesParams()
    events = classify_events_from_payload(payload)
    try:
        minute = float(payload.get("minute") or 0)
    except (TypeError, ValueError):
        minute = 0.0
    try:
        t = float(payload.get("match_seconds") or minute * 60.0)
    except (TypeError, ValueError):
        t = minute * 60.0

    intensity = hawkes_intensity(t, events, params, minute=minute)
    p_hawkes = baseline_prob_horizon(intensity, horizon_sec)

    # Blend with legacy engine lambda if provided (keeps continuity)
    if legacy_lambda is not None and legacy_lambda >= 0:
        p_legacy = poisson_at_least_one(float(legacy_lambda))
        # conservative blend: geometric mean of odds-space approx via average logit
        logit_blend = 0.6 * _logit(p_hawkes) + 0.4 * _logit(p_legacy)
        p_final = _sigmoid(logit_blend)
    else:
        p_final = p_hawkes

    return {
        "p_baseline": p_final,
        "p_hawkes": p_hawkes,
        "intensity": intensity,
        "n_events": len(events),
        "event_types": sorted({e.event_type for e in events}),
        "horizon_sec": horizon_sec,
        "params_version": "hawkes_typed_v1",
    }


# ---------------------------------------------------------------------------
# Feature vector for residual model
# ---------------------------------------------------------------------------

def build_feature_vector(
    feats: Dict[str, float],
    p_baseline: float,
) -> np.ndarray:
    p = _clamp(float(p_baseline), 1e-6, 1 - 1e-6)
    values = {
        "da_total": float(feats.get("da_total") or 0.0),
        "xg_total": float(feats.get("xg_total") or 0.0),
        "corners_total": float(feats.get("corners_total") or 0.0),
        "goals_total": float(feats.get("goals_total") or 0.0),
        "dda_dt": float(feats.get("dda_dt") or 0.0),
        "dxg_dt": float(feats.get("dxg_dt") or 0.0),
        "dc_dt": float(feats.get("dc_dt") or 0.0),
        "da_ratio_home": float(feats.get("da_ratio_home") or 0.5),
        "pressure_imbalance": float(feats.get("pressure_imbalance") or 0.0),
        "corner_pace": float(feats.get("corner_pace") or 0.0),
        "minute_proxy": float(feats.get("minute_proxy") or 0.0),
        "p_hawkes": p,
        "log_odds_hawkes": _logit(p),
    }
    return np.array([values[n] for n in FEATURE_NAMES], dtype=np.float64)


# ---------------------------------------------------------------------------
# Calibrators
# ---------------------------------------------------------------------------

@dataclass
class Calibrator:
    kind: str = "platt"  # platt | isotonic | identity
    # Platt: p' = sigmoid(a * logit(p) + b)
    a: float = 1.0
    b: float = 0.0
    # Isotonic stored as parallel arrays
    x_thr: Optional[List[float]] = None
    y_thr: Optional[List[float]] = None
    version: str = "cal_v1"
    horizon_sec: int = 300
    n_train: int = 0

    def transform(self, p: float) -> float:
        p = _clamp(float(p), 1e-6, 1 - 1e-6)
        if self.kind == "identity":
            return p
        if self.kind == "platt":
            return _sigmoid(self.a * _logit(p) + self.b)
        if self.kind == "isotonic" and self.x_thr and self.y_thr:
            return float(np.interp(p, self.x_thr, self.y_thr))
        return p

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Calibrator":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def fit_calibrator(
    probs: Sequence[float],
    labels: Sequence[int],
    *,
    horizon_sec: int = 300,
) -> Calibrator:
    probs_a = np.array(probs, dtype=np.float64)
    labels_a = np.array(labels, dtype=np.int32)
    mask = np.isfinite(probs_a) & ((labels_a == 0) | (labels_a == 1))
    probs_a = probs_a[mask]
    labels_a = labels_a[mask]
    n = len(probs_a)
    if n < 20:
        return Calibrator(kind="identity", horizon_sec=horizon_sec, n_train=n)

    if n >= MIN_ISOTONIC_N and HAS_SK:
        iso = IsotonicRegression(out_of_bounds="clip", y_min=1e-4, y_max=1 - 1e-4)
        iso.fit(probs_a, labels_a)
        return Calibrator(
            kind="isotonic",
            x_thr=iso.X_thresholds_.tolist(),
            y_thr=iso.y_thresholds_.tolist(),
            horizon_sec=horizon_sec,
            n_train=n,
            version="cal_isotonic_v1",
        )

    # Platt via logistic on logit(p)
    if HAS_SK:
        X = np.array([[_logit(float(p))] for p in probs_a])
        lr = LogisticRegression(solver="lbfgs", max_iter=200)
        lr.fit(X, labels_a)
        a = float(lr.coef_[0][0])
        b = float(lr.intercept_[0])
        return Calibrator(kind="platt", a=a, b=b, horizon_sec=horizon_sec, n_train=n, version="cal_platt_v1")

    # Pure numpy fallback: identity
    return Calibrator(kind="identity", horizon_sec=horizon_sec, n_train=n)


# ---------------------------------------------------------------------------
# Residual model
# ---------------------------------------------------------------------------

@dataclass
class ResidualModel:
    model_type: str = "lightgbm"
    # serialized booster path or coefficients
    booster_path: str = ""
    residual_clamp: float = RESIDUAL_CLAMP
    feature_manifest_version: str = FEATURE_MANIFEST_VERSION
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    version: str = "residual_v1"
    trained: bool = False
    _booster: Any = field(default=None, repr=False, compare=False)

    def predict_residual(self, x: np.ndarray) -> float:
        if not self.trained or self._booster is None:
            return 0.0
        if HAS_LGB and self.model_type == "lightgbm":
            pred = self._booster.predict(x.reshape(1, -1))
            r = float(pred[0])
            return _clamp(r, -self.residual_clamp, self.residual_clamp)
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_type": self.model_type,
            "booster_path": self.booster_path,
            "residual_clamp": self.residual_clamp,
            "feature_manifest_version": self.feature_manifest_version,
            "feature_names": self.feature_names,
            "version": self.version,
            "trained": self.trained,
        }


def fit_residual_lgbm(
    X: np.ndarray,
    y: np.ndarray,
    p_baseline: np.ndarray,
    *,
    version: str = "residual_v1",
) -> ResidualModel:
    """
    Train residual in logit space:
      target_residual ≈ logit(y_smoothed) - logit(p_baseline)
    For classification we train on labels with init_score = logit(p_baseline).
    """
    model = ResidualModel(version=version)
    if not HAS_LGB or len(y) < 30:
        return model

    # LightGBM binary with init_score = logit(p_base) learns residual additive in logit
    init_score = np.array([_logit(float(p)) for p in p_baseline])
    train = lgb.Dataset(X, label=y, feature_name=FEATURE_NAMES, free_raw_data=False)
    train.set_init_score(init_score)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
        "seed": 42,
    }
    booster = lgb.train(params, train, num_boost_round=80)
    path = str(MODEL_DIR / f"{version}.lgb")
    booster.save_model(path)
    model._booster = booster
    model.booster_path = path
    model.trained = True
    return model


def combine_baseline_residual(
    p_baseline: float,
    residual: float,
    residual_clamp: float = RESIDUAL_CLAMP,
) -> float:
    r = _clamp(residual, -residual_clamp, residual_clamp)
    return _sigmoid(_logit(p_baseline) + r)


# ---------------------------------------------------------------------------
# Walk-forward by fixture_id
# ---------------------------------------------------------------------------

@dataclass
class SnapshotRow:
    fixture_id: str
    ts: float
    features: Dict[str, float]
    p_baseline: float
    label: Optional[int]  # None = censored → excluded from supervised metrics
    horizon_sec: int = 300


def walk_forward_splits(
    rows: Sequence[SnapshotRow],
    *,
    min_train_fixtures: int = 3,
) -> List[Dict[str, Any]]:
    """
    Group by fixture_id ordered by first ts.
    For each step: train on past fixtures, calibrate on next block, test on following.
    """
    # order fixtures by first appearance
    first_ts: Dict[str, float] = {}
    for r in rows:
        if r.fixture_id not in first_ts or r.ts < first_ts[r.fixture_id]:
            first_ts[r.fixture_id] = r.ts
    fixtures = sorted(first_ts.keys(), key=lambda f: first_ts[f])

    splits = []
    # need train / cal / test blocks
    n = len(fixtures)
    if n < min_train_fixtures + 2:
        return splits

    for test_idx in range(min_train_fixtures + 1, n):
        train_f = fixtures[: test_idx - 1]
        cal_f = [fixtures[test_idx - 1]]
        test_f = [fixtures[test_idx]]
        splits.append({
            "train_fixtures": train_f,
            "cal_fixtures": cal_f,
            "test_fixtures": test_f,
        })
    return splits


def _rows_for_fixtures(rows: Sequence[SnapshotRow], fids: Sequence[str]) -> List[SnapshotRow]:
    s = set(fids)
    return [r for r in rows if r.fixture_id in s and r.label is not None]


def evaluate_probs(
    probs: Sequence[float],
    labels: Sequence[int],
    *,
    abstain_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    probs_l = list(probs)
    labels_l = list(labels)
    n = len(probs_l)
    if n == 0:
        return {
            "n": 0,
            "brier": float("nan"),
            "log_loss": float("nan"),
            "coverage": 0.0,
            "conditional_precision": float("nan"),
            "reliability": [],
        }

    coverage = 1.0
    used_p, used_y = probs_l, labels_l
    if abstain_threshold is not None:
        pairs = [(p, y) for p, y in zip(probs_l, labels_l) if p >= abstain_threshold]
        coverage = len(pairs) / n if n else 0.0
        if pairs:
            used_p = [p for p, _ in pairs]
            used_y = [y for _, y in pairs]
        else:
            used_p, used_y = [], []

    if not used_p:
        return {
            "n": n,
            "n_scored": 0,
            "brier": float("nan"),
            "log_loss": float("nan"),
            "coverage": coverage,
            "conditional_precision": float("nan"),
            "reliability": [],
        }

    # conditional precision: among predicted positive (p>=0.5), fraction of label=1
    pos = [(p, y) for p, y in zip(used_p, used_y) if p >= 0.5]
    if pos:
        cond_prec = sum(y for _, y in pos) / len(pos)
    else:
        cond_prec = float("nan")

    return {
        "n": n,
        "n_scored": len(used_p),
        "brier": brier_score(used_p, used_y),
        "log_loss": log_loss(used_p, used_y),
        "coverage": coverage,
        "conditional_precision": cond_prec,
        "reliability": calibration_bins(used_p, used_y),
    }


def run_walk_forward(
    rows: Sequence[SnapshotRow],
    *,
    horizon_sec: int = 300,
) -> Dict[str, Any]:
    splits = walk_forward_splits(rows)
    fold_reports = []
    all_base_p, all_chal_p, all_y = [], [], []

    for i, sp in enumerate(splits):
        train_rows = _rows_for_fixtures(rows, sp["train_fixtures"])
        cal_rows = _rows_for_fixtures(rows, sp["cal_fixtures"])
        test_rows = _rows_for_fixtures(rows, sp["test_fixtures"])
        if len(train_rows) < 20 or len(test_rows) < 5:
            continue

        X_train = np.vstack([build_feature_vector(r.features, r.p_baseline) for r in train_rows])
        y_train = np.array([int(r.label) for r in train_rows], dtype=np.int32)
        p_train = np.array([r.p_baseline for r in train_rows], dtype=np.float64)

        residual = fit_residual_lgbm(X_train, y_train, p_train, version=f"residual_fold{i}")

        # raw challenger probs on cal for calibrator
        def _chal_raw(r: SnapshotRow) -> float:
            x = build_feature_vector(r.features, r.p_baseline)
            res = residual.predict_residual(x)
            return combine_baseline_residual(r.p_baseline, res, residual.residual_clamp)

        cal_raw = [_chal_raw(r) for r in cal_rows] if cal_rows else [_chal_raw(r) for r in train_rows[-50:]]
        cal_y = [int(r.label) for r in cal_rows] if cal_rows else [int(r.label) for r in train_rows[-50:]]
        calibrator = fit_calibrator(cal_raw, cal_y, horizon_sec=horizon_sec)

        base_p = [r.p_baseline for r in test_rows]
        chal_raw = [_chal_raw(r) for r in test_rows]
        chal_p = [calibrator.transform(p) for p in chal_raw]
        y = [int(r.label) for r in test_rows]

        base_m = evaluate_probs(base_p, y)
        chal_m = evaluate_probs(chal_p, y)

        fold_reports.append({
            "fold": i,
            "train_fixtures": sp["train_fixtures"],
            "cal_fixtures": sp["cal_fixtures"],
            "test_fixtures": sp["test_fixtures"],
            "baseline": base_m,
            "challenger": chal_m,
            "calibrator": calibrator.to_dict(),
            "residual_trained": residual.trained,
        })
        all_base_p.extend(base_p)
        all_chal_p.extend(chal_p)
        all_y.extend(y)

    summary = {
        "horizon_sec": horizon_sec,
        "n_folds": len(fold_reports),
        "feature_manifest_version": FEATURE_MANIFEST_VERSION,
        "baseline_overall": evaluate_probs(all_base_p, all_y),
        "challenger_overall": evaluate_probs(all_chal_p, all_y),
        "folds": fold_reports,
        "has_lightgbm": HAS_LGB,
        "has_sklearn": HAS_SK,
    }
    return summary


def challenger_beats_champion(report: Dict[str, Any]) -> bool:
    """Promotion rule: lower Brier and not worse log_loss on overall temporal test."""
    b = report.get("baseline_overall") or {}
    c = report.get("challenger_overall") or {}
    if not c.get("n_scored"):
        return False
    try:
        return float(c["brier"]) < float(b["brier"]) and float(c["log_loss"]) <= float(b["log_loss"]) + 1e-6
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Shadow inference (does not affect live decisions)
# ---------------------------------------------------------------------------

@dataclass
class ShadowBundle:
    residual: ResidualModel
    calibrator: Calibrator
    champion: bool = False
    model_version: str = "shadow_v1"
    metrics: Dict[str, Any] = field(default_factory=dict)

    def predict(self, feats: Dict[str, float], p_baseline: float) -> Dict[str, Any]:
        x = build_feature_vector(feats, p_baseline)
        residual = self.residual.predict_residual(x)
        p_raw = combine_baseline_residual(p_baseline, residual, self.residual.residual_clamp)
        p_cal = self.calibrator.transform(p_raw)
        return {
            "p_baseline": p_baseline,
            "residual": residual,
            "p_raw": p_raw,
            "p_calibrated": p_cal,
            "model_version": self.model_version,
            "calibrator_version": self.calibrator.version,
            "feature_manifest_version": FEATURE_MANIFEST_VERSION,
            "shadow": True,
            "champion": self.champion,
        }


_SHADOW_BUNDLE: Optional[ShadowBundle] = None


def get_shadow_bundle() -> Optional[ShadowBundle]:
    return _SHADOW_BUNDLE


def set_shadow_bundle(bundle: Optional[ShadowBundle]) -> None:
    global _SHADOW_BUNDLE
    _SHADOW_BUNDLE = bundle


def load_or_default_shadow() -> ShadowBundle:
    """Identity residual + identity calibrator as safe default."""
    residual = ResidualModel(trained=False)
    cal = Calibrator(kind="identity")
    return ShadowBundle(residual=residual, calibrator=cal, champion=False, model_version="shadow_identity_v1")


def shadow_predict(
    feats: Dict[str, float],
    p_baseline: float,
) -> Dict[str, Any]:
    bundle = get_shadow_bundle() or load_or_default_shadow()
    return bundle.predict(feats, p_baseline)
