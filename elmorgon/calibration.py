"""Conformal calibration of the P10 to P90 interval (conformalized quantile regression).

Raw quantile models are overconfident on spiky prices: in the walk-forward
backtest the raw 80 % interval held only ~60 % of outcomes. For each zone,
the interval is widened (or narrowed) by the 80 % quantile of how far
recent actuals fell outside it, measured only on out-of-sample predictions
made before the day being forecast (Romano, Patterson & Candès, 2019).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

WINDOW_DAYS = 90
TARGET_COVERAGE = 0.8
MIN_POINTS = 24 * 30  # a month of hours before a margin is trusted


def margin(history: pd.DataFrame) -> float:
    """Additive margin from past rows with target_price, q10 and q90 (0 when too few)."""
    if len(history) < MIN_POINTS:
        return 0.0
    scores = np.maximum(
        history["q10"] - history["target_price"], history["target_price"] - history["q90"]
    ).to_numpy()
    n = len(scores)
    level = min(1.0, math.ceil((n + 1) * TARGET_COVERAGE) / n)
    return float(np.quantile(scores, level, method="higher"))


def window(history: pd.DataFrame, before: date) -> pd.DataFrame:
    """The WINDOW_DAYS of history that end the day before `before`."""
    days = history["delivery_date"]
    return history[(days < before) & (days >= before - timedelta(days=WINDOW_DAYS))]


def apply(rows: pd.DataFrame, m: float) -> pd.DataFrame:
    """Move q10 down and q90 up by the margin, never past the median."""
    out = rows.copy()
    out["q10"] = np.minimum(out["q10"] - m, out["q50"])
    out["q90"] = np.maximum(out["q90"] + m, out["q50"])
    return out


def calibrate_backtest(
    preds: pd.DataFrame, models: tuple[str, ...] = ("gbm", "gbm_no_weather")
) -> pd.DataFrame:
    """Calibrate each fold with margins from earlier folds only."""
    preds = preds.copy()
    preds["q10_raw"], preds["q90_raw"] = preds["q10"], preds["q90"]
    preds["interval_margin"] = 0.0
    for _key, group in preds[preds["model"].isin(models)].groupby(["model", "zone"]):
        raw = group[["delivery_date", "target_price", "q10_raw", "q90_raw"]].rename(
            columns={"q10_raw": "q10", "q90_raw": "q90"}
        )
        for fold, fold_rows in group.groupby("fold"):
            m = margin(window(raw, fold))
            idx = fold_rows.index
            preds.loc[idx, ["q10", "q90"]] = apply(fold_rows, m)[["q10", "q90"]].to_numpy()
            preds.loc[idx, "interval_margin"] = m
    return preds
