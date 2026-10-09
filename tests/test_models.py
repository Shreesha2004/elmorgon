"""Models, the walk-forward backtest, interval calibration and live issuing."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from elmorgon import backtest, calibration, forecast, models
from elmorgon.backtest import pinball, summarise


def synthetic_features(
    days: int = 120, start: date = date(2024, 1, 1), seed: int = 1
) -> pd.DataFrame:
    """A small feature table with the same columns as feat_price_hourly."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        for zone in models.ZONE_CODES:
            for h in range(24):
                base = 0.5 + 0.3 * np.sin(h / 24 * 6.28) + 0.1 * models.ZONE_CODES[zone]
                rows.append(
                    {
                        "zone": zone,
                        "delivery_date": day,
                        "issue_date": day - timedelta(days=1),
                        "hour_start": pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=h),
                        "hour_local": h,
                        "target_price": base + rng.normal(0, 0.05),
                    }
                )
    frame = pd.DataFrame(rows)
    for col in models.CALENDAR + models.PRICE_HISTORY + models.WEATHER:
        if col not in frame:
            frame[col] = rng.normal(0, 1, len(frame))
    frame["lag_1d"] = frame["target_price"] + rng.normal(0, 0.1, len(frame))
    frame["weather_source"] = "archive"
    return frame


def interval_history(days: int, width: float, noise: float, seed: int = 0, start=date(2025, 1, 1)):
    """Past predictions with a fixed interval of +/- width around 0, and noisy actuals."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "delivery_date": np.repeat([start + timedelta(days=d) for d in range(days)], 24),
            "target_price": rng.normal(0, noise, days * 24),
            "q10": -width,
            "q50": 0.0,
            "q90": width,
        }
    )


def coverage(frame):
    return (
        (frame["target_price"] >= frame["q10"]) & (frame["target_price"] <= frame["q90"])
    ).mean()


# Models and metrics


def test_naive_day_repeats_yesterday():
    rows = synthetic_features(3)
    pred = models.naive_day(rows)
    assert (pred["q50"] == rows["lag_1d"]).all()
    assert (pred["q10"] == pred["q90"]).all()


def test_gbm_quantiles_are_ordered_and_beat_noise():
    rows = synthetic_features(90)
    train = rows[rows.delivery_date < date(2024, 3, 15)]
    test = rows[rows.delivery_date >= date(2024, 3, 15)]
    model = models.QuantileGBM().fit(train)
    pred = model.predict(test)
    assert (pred["q10"] <= pred["q50"]).all() and (pred["q50"] <= pred["q90"]).all()
    mae = (pred["q50"] - test["target_price"]).abs().mean()
    assert mae < test["target_price"].std()
    assert model.importance().sum() == pytest.approx(1.0)


def test_pinball_loss():
    y = np.array([1.0, 2.0])
    assert pinball(y, np.array([0.0, 0.0]), 0.9) == pytest.approx(0.9 * 1.5)
    assert pinball(y, np.array([3.0, 3.0]), 0.9) == pytest.approx(0.1 * 1.5)


def test_summary_metrics():
    frame = pd.DataFrame(
        {
            "target_price": [1.0, 2.0, 3.0],
            "q10": [0.5, 2.5, 2.0],
            "q50": [1.0, 2.5, 3.0],
            "q90": [1.5, 3.0, 4.0],
        }
    )
    s = summarise(frame, baseline_mae=1.0)
    assert s["mae"] == pytest.approx(1 / 6)
    assert s["coverage_80"] == pytest.approx(2 / 3)
    assert s["skill_vs_naive_day"] == pytest.approx(1 - 1 / 6)


# Backtest


def test_backtest_never_trains_on_the_month_it_predicts(monkeypatch, tmp_path):
    last_training_day = []

    class Spy(models.QuantileGBM):
        def fit(self, rows):
            last_training_day.append(rows["delivery_date"].max())
            return super().fit(rows)

    monkeypatch.setattr(models, "QuantileGBM", Spy)
    monkeypatch.setattr(backtest, "PREDICTIONS", tmp_path / "predictions.parquet")
    preds = backtest.run(synthetic_features(100), start=date(2024, 3, 1))

    folds = sorted(set(preds["fold"]))
    assert folds == [date(2024, 3, 1), date(2024, 4, 1)]
    # Two LightGBM models are fitted per fold, in fold order.
    for fold, last_day in zip([f for f in folds for _ in range(2)], last_training_day, strict=True):
        assert last_day < fold
    assert set(preds["model"]) == {"naive_day", "naive_week", "gbm_no_weather", "gbm"}


def test_month_starts():
    assert backtest.month_starts(date(2024, 11, 15), date(2025, 2, 1)) == [
        date(2024, 11, 1),
        date(2024, 12, 1),
        date(2025, 1, 1),
        date(2025, 2, 1),
    ]


# Calibration


def test_too_narrow_intervals_are_widened_to_target_coverage():
    m = calibration.margin(interval_history(90, width=0.2, noise=1.0))
    assert m > 0
    future = calibration.apply(interval_history(60, width=0.2, noise=1.0, seed=1), m)
    assert coverage(future) == pytest.approx(0.8, abs=0.03)


def test_too_wide_intervals_are_narrowed():
    assert calibration.margin(interval_history(90, width=5.0, noise=1.0)) < 0


def test_quantiles_never_cross_after_narrowing():
    out = calibration.apply(interval_history(40, width=0.1, noise=1.0), -1.0)
    assert (out["q10"] <= out["q50"]).all() and (out["q50"] <= out["q90"]).all()


def test_too_little_history_gives_no_margin():
    assert calibration.margin(interval_history(5, width=0.2, noise=1.0)) == 0.0


def test_window_only_uses_days_before_the_forecast():
    w = calibration.window(interval_history(200, width=0.2, noise=1.0), date(2025, 6, 1))
    assert w["delivery_date"].max() < date(2025, 6, 1)
    assert w["delivery_date"].min() >= date(2025, 6, 1) - timedelta(days=calibration.WINDOW_DAYS)


def test_backtest_folds_are_calibrated_from_earlier_folds_only():
    hist = interval_history(150, width=0.2, noise=1.0)
    hist["zone"], hist["model"] = "SE3", "gbm"
    hist["fold"] = hist["delivery_date"].map(lambda d: d.replace(day=1))
    out = calibration.calibrate_backtest(hist)
    first_fold = out[out["fold"] == date(2025, 1, 1)]
    assert (first_fold["interval_margin"] == 0).all()  # nothing earlier to learn from
    later = out[out["fold"] >= date(2025, 3, 1)]
    assert (later["interval_margin"] > 0).all()
    assert coverage(later) == pytest.approx(0.8, abs=0.03)


# Live forecasts


def test_live_forecast_is_written_once_and_never_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(forecast, "ISSUED", tmp_path)
    monkeypatch.setattr(backtest, "PREDICTIONS", tmp_path / "no-backtest-yet.parquet")
    features = synthetic_features(40)
    last = features["delivery_date"].max()
    features.loc[features["delivery_date"] == last, "target_price"] = np.nan

    path = forecast.issue(features)
    assert path == tmp_path / f"{last}.parquet"
    first = pd.read_parquet(path)
    assert len(first) == 4 * 24
    assert {"q10", "q50", "q90", "naive_day", "issued_at", "trained_through"} <= set(first)
    assert first["trained_through"].iloc[0] < last

    assert forecast.issue(features) is None
    pd.testing.assert_frame_equal(pd.read_parquet(path), first)


def test_issued_forecasts_load_in_date_order(tmp_path, monkeypatch):
    monkeypatch.setattr(forecast, "ISSUED", tmp_path)
    for d in (date(2025, 1, 3), date(2025, 1, 2)):
        pd.DataFrame({"delivery_date": [d], "zone": ["SE3"]}).to_parquet(tmp_path / f"{d}.parquet")
    assert forecast.load_issued()["delivery_date"].tolist() == [date(2025, 1, 2), date(2025, 1, 3)]
    assert forecast.issued_path(date(2025, 1, 2)).name == "2025-01-02.parquet"
    monkeypatch.setattr(forecast, "ISSUED", tmp_path / "empty")
    assert forecast.load_issued().empty
