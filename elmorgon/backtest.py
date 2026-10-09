"""Walk-forward backtest and its error metrics.

At the start of each month from BACKTEST_START, every model is retrained on
all rows delivered before that month and then predicts the month. The feature
table is point-in-time correct, so predicting a whole month with one model
uses nothing a 10:00 forecaster would not have had on each issue day.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date

import numpy as np
import pandas as pd

from elmorgon import calibration, models
from elmorgon.config import BACKTEST_START, FORECASTS

log = logging.getLogger(__name__)

PREDICTIONS = FORECASTS / "backtest_predictions.parquet"
METRICS = FORECASTS / "backtest_metrics.json"

MODEL_LABELS = {
    "naive_day": "Same hour yesterday",
    "naive_week": "Same hour last week",
    "gbm_no_weather": "LightGBM, prices and calendar only",
    "gbm": "LightGBM with weather (Elmorgon)",
}


def next_month(m: date) -> date:
    return date(m.year + (m.month == 12), m.month % 12 + 1, 1)


def month_starts(first: date, last: date) -> list[date]:
    months, m = [], first.replace(day=1)
    while m <= last:
        months.append(m)
        m = next_month(m)
    return months


def run(features: pd.DataFrame, start: date = BACKTEST_START) -> pd.DataFrame:
    """Predict every month from `start` with models trained only on earlier months."""
    known = features[features["target_price"].notna()].copy()
    folds = []
    for m in month_starts(start, known["delivery_date"].max()):
        train = known[known["delivery_date"] < m]
        test = known[(known["delivery_date"] >= m) & (known["delivery_date"] < next_month(m))]
        if test.empty:
            continue
        predictors: dict[str, Callable[[pd.DataFrame], pd.DataFrame]] = {
            "naive_day": models.naive_day,
            "naive_week": models.naive_week,
            "gbm_no_weather": models.QuantileGBM(weather=False).fit(train).predict,
            "gbm": models.QuantileGBM(weather=True).fit(train).predict,
        }
        for name, predict in predictors.items():
            fold = test[
                ["zone", "delivery_date", "hour_start", "hour_local", "target_price"]
            ].copy()
            fold[list(models.QCOLS)] = predict(test).to_numpy()
            fold["model"] = name
            fold["fold"] = m
            folds.append(fold)
        log.info("backtest fold %s: trained on %d rows, predicted %d", m, len(train), len(test))
    # Intervals are calibrated per fold with errors from earlier folds only.
    out = calibration.calibrate_backtest(pd.concat(folds, ignore_index=True))
    PREDICTIONS.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(PREDICTIONS, index=False)
    return out


def pinball(y: np.ndarray, pred: np.ndarray, q: float) -> float:
    """Quantile loss: under- and over-forecasts are weighted by q and 1 - q."""
    diff = y - pred
    return float(np.mean(np.maximum(q * diff, (q - 1) * diff)))


def summarise(frame: pd.DataFrame, baseline_mae: float | None = None) -> dict[str, float]:
    """Point and interval metrics for rows with columns target_price, q10, q50, q90."""
    y = frame["target_price"].to_numpy()
    err = frame["q50"].to_numpy() - y
    quantile_losses = [
        pinball(y, frame[col].to_numpy(), q)
        for col, q in zip(models.QCOLS, models.QUANTILES, strict=True)
    ]
    out = {
        "n": int(len(frame)),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "bias": float(np.mean(err)),
        "pinball": float(np.mean(quantile_losses)),
        "coverage_80": float(np.mean((y >= frame["q10"]) & (y <= frame["q90"]))),
    }
    if baseline_mae:
        out["skill_vs_naive_day"] = 1 - out["mae"] / baseline_mae
    return out


def score(predictions: pd.DataFrame) -> dict:
    """Metrics by model, overall, per zone and per year; written to METRICS."""
    result: dict = {"models": MODEL_LABELS, "overall": {}, "by_zone": {}, "by_year": {}}
    preds = predictions.copy()
    preds["year"] = pd.to_datetime(preds["delivery_date"]).dt.year

    def base_mae(subset: pd.DataFrame) -> float:
        b = subset[subset["model"] == "naive_day"]
        return float((b["q50"] - b["target_price"]).abs().mean())

    for name, group in preds.groupby("model"):
        result["overall"][name] = summarise(group, base_mae(preds))
        if "q10_raw" in group and name.startswith("gbm"):
            y = group["target_price"]
            raw_inside = (y >= group["q10_raw"]) & (y <= group["q90_raw"])
            result["overall"][name]["coverage_80_uncalibrated"] = float(raw_inside.mean())
    for zone, zgroup in preds.groupby("zone"):
        result["by_zone"][zone] = {
            name: summarise(g, base_mae(zgroup)) for name, g in zgroup.groupby("model")
        }
    for year, ygroup in preds.groupby("year"):
        result["by_year"][str(year)] = {
            name: summarise(g, base_mae(ygroup)) for name, g in ygroup.groupby("model")
        }
    monthly = (
        preds.assign(abs_err=(preds["q50"] - preds["target_price"]).abs())
        .groupby(["fold", "model"])["abs_err"]
        .mean()
        .unstack("model")
    )
    result["monthly_mae"] = [
        {"month": str(idx)[:7], **{k: round(float(v), 4) for k, v in row.items()}}
        for idx, row in monthly.iterrows()
    ]
    result["period"] = {
        "first_day": str(preds["delivery_date"].min()),
        "last_day": str(preds["delivery_date"].max()),
        "days": int(preds["delivery_date"].nunique()),
    }
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    METRICS.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
