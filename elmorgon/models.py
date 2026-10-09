"""Forecasting models: two naive baselines and a LightGBM quantile model.

Every model takes rows of the feature table and returns, for each row, the
10th, 50th and 90th percentile of the hourly price.
"""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

QUANTILES = (0.1, 0.5, 0.9)
QCOLS = ("q10", "q50", "q90")

CALENDAR = ["hour_local", "iso_dow", "month", "is_weekend", "is_holiday", "is_workday"]
PRICE_HISTORY = [
    "lag_1d",
    "lag_2d",
    "lag_7d",
    "prev_day_mean",
    "prev_day_min",
    "prev_day_max",
    "prev_day_std",
    "prev_day_peak_mean",
    "prev_week_mean",
    "prev_day_mean_change",
    "prev_se1_mean",
    "prev_se3_mean",
    "prev_se4_mean",
]
WEATHER = [
    "temperature_c",
    "wind_100m_ms",
    "solar_w_m2",
    "cloud_cover_pct",
    "north_wind_ms",
    "target_day_temp_c",
    "target_day_wind_ms",
]
ZONE_CODES = {"SE1": 0, "SE2": 1, "SE3": 2, "SE4": 3}


def design_matrix(rows: pd.DataFrame, weather: bool = True) -> pd.DataFrame:
    """Model inputs, with the zone as an integer category."""
    cols = CALENDAR + PRICE_HISTORY + (WEATHER if weather else [])
    x = rows[cols].astype("float64").copy()
    x.insert(0, "zone", rows["zone"].map(ZONE_CODES).astype("int64"))
    return x


def naive_day(rows: pd.DataFrame) -> pd.DataFrame:
    """Tomorrow looks like today: the same local hour on the issue day."""
    return _point(rows, rows["lag_1d"].fillna(rows["prev_day_mean"]))


def naive_week(rows: pd.DataFrame) -> pd.DataFrame:
    """Tomorrow looks like the same weekday last week."""
    return _point(rows, rows["lag_7d"].fillna(rows["prev_week_mean"]))


def _point(rows: pd.DataFrame, values: pd.Series) -> pd.DataFrame:
    v = values.to_numpy()
    return pd.DataFrame({"q10": v, "q50": v, "q90": v}, index=rows.index)


class QuantileGBM:
    """One LightGBM model per quantile, shared across all four zones."""

    params = dict(
        n_estimators=500,
        learning_rate=0.04,
        num_leaves=48,
        min_child_samples=40,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        verbose=-1,
        n_jobs=-1,
    )

    def __init__(
        self, weather: bool = True, quantiles: tuple[float, ...] = QUANTILES, seed: int = 7
    ):
        self.weather = weather
        self.quantiles = quantiles
        self.seed = seed
        self.models: dict[float, lgb.LGBMRegressor] = {}

    def fit(self, rows: pd.DataFrame) -> QuantileGBM:
        x = design_matrix(rows, self.weather)
        y = rows["target_price"].to_numpy()
        for q in self.quantiles:
            model = lgb.LGBMRegressor(
                objective="quantile", alpha=q, random_state=self.seed, **self.params
            )
            model.fit(x, y, categorical_feature=["zone"])
            self.models[q] = model
        return self

    def predict(self, rows: pd.DataFrame) -> pd.DataFrame:
        x = design_matrix(rows, self.weather)
        preds = np.column_stack([self.models[q].predict(x) for q in self.quantiles])
        # Separate quantile models can cross; sorting restores q10 <= q50 <= q90.
        preds.sort(axis=1)
        if len(self.quantiles) == 1:
            preds = np.repeat(preds, 3, axis=1)
        return pd.DataFrame(preds, columns=list(QCOLS), index=rows.index)

    def importance(self) -> pd.Series:
        """Gain importance of the median model, normalised to sum to 1."""
        model = self.models[0.5] if 0.5 in self.models else next(iter(self.models.values()))
        gain = pd.Series(
            model.booster_.feature_importance("gain"), index=model.booster_.feature_name()
        )
        return (gain / gain.sum()).sort_values(ascending=False)
