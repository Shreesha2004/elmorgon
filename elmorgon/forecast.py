"""Live forecasts: issue once per delivery day, never rewrite, score when prices arrive."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from elmorgon import backtest, calibration, models, warehouse
from elmorgon.config import ISSUED

log = logging.getLogger(__name__)

MODEL_VERSION = "gbm-quantile-cqr-v1"


def issued_path(target_date) -> Path:
    return ISSUED / f"{target_date}.parquet"


def issue(features: pd.DataFrame | None = None) -> Path | None:
    """Forecast the first delivery day whose prices are not published yet.

    Returns the file written, or None if that day was already forecast: a
    forecast is final once issued, so a re-run can never improve it in hindsight.
    """
    features = warehouse.load_features() if features is None else features
    live = features[features["target_price"].isna()]
    if live.empty:
        log.info("no unpublished delivery day to forecast")
        return None
    target_date = live["delivery_date"].min()
    live = live[live["delivery_date"] == target_date]
    path = issued_path(target_date)
    if path.exists():
        log.info("forecast for %s already issued; leaving it untouched", target_date)
        return None

    known = features[features["target_price"].notna()]
    model = models.QuantileGBM().fit(known)
    out = live[["zone", "delivery_date", "hour_start", "hour_local", "weather_source"]].copy()
    out[list(models.QCOLS)] = model.predict(live).to_numpy()
    margins = recent_margins()
    out["interval_margin"] = out["zone"].map(margins).fillna(0.0)
    for zone, rows in out.groupby("zone"):
        out.loc[rows.index, ["q10", "q90"]] = calibration.apply(rows, margins.get(zone, 0.0))[
            ["q10", "q90"]
        ].to_numpy()
    out["naive_day"] = models.naive_day(live)["q50"].to_numpy()
    out["issued_at"] = pd.Timestamp(datetime.now(UTC))
    out["model_version"] = MODEL_VERSION
    out["trained_through"] = known["delivery_date"].max()

    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)
    log.info("issued forecast for %s (%d rows)", target_date, len(out))
    return path


def recent_margins() -> dict[str, float]:
    """Per-zone interval margin from the latest 90 days of out-of-sample backtest errors."""
    if not backtest.PREDICTIONS.exists():
        return {}
    hist = pd.read_parquet(backtest.PREDICTIONS)
    hist = hist[hist["model"] == "gbm"]
    if "q10_raw" in hist:
        hist = hist.drop(columns=["q10", "q90"]).rename(
            columns={"q10_raw": "q10", "q90_raw": "q90"}
        )
    end = hist["delivery_date"].max() + timedelta(days=1)
    return {
        zone: calibration.margin(calibration.window(g, end)) for zone, g in hist.groupby("zone")
    }


def load_issued() -> pd.DataFrame:
    """Every issued forecast, oldest first."""
    files = sorted(ISSUED.glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def track_record() -> pd.DataFrame:
    """Issued forecasts joined to the prices that cleared (only days that have cleared)."""
    issued = load_issued()
    if issued.empty:
        return issued
    actual = warehouse.query("select zone, hour_start, price_sek_kwh from fct_price_hourly")
    joined = issued.merge(actual, on=["zone", "hour_start"], how="inner")
    return joined.rename(columns={"price_sek_kwh": "actual"})
