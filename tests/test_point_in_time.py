"""The feature table must not leak the prices it is asked to forecast.

Builds the real dbt project on three weeks of synthetic data, then multiplies
one delivery day's prices by ten and rebuilds. If any feature for that day
changes, a feature was looking at the answer.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from elmorgon import silver, warehouse
from elmorgon.config import ZONES
from tests.conftest import price_records, weather_payload, write_json

START, DAYS = date(2024, 1, 1), 21
TARGET = START + timedelta(days=14)  # the day whose prices get shocked


def _write_bronze(root, shock: float = 1.0) -> None:
    for d in range(DAYS):
        day = START + timedelta(days=d)
        for zone in ZONES:
            scale = shock if day == TARGET else 1.0
            write_json(
                root / f"bronze/prices/{zone}/{day:%Y}/{day}.json",
                price_records(day, seed=ZONES.index(zone), scale=scale),
            )
    last = START + timedelta(days=DAYS - 1)
    write_json(root / "bronze/weather/archive/2024-01.json", weather_payload(START, last))
    write_json(
        root / f"bronze/weather/forecast/{last}.json",
        {
            "fetched_at": f"{last}T10:00:00+01:00",
            "points": weather_payload(last, last + timedelta(days=2)),
        },
    )


def _build(root) -> pd.DataFrame:
    for sub in ("silver", "quarantine"):
        shutil.rmtree(root / sub, ignore_errors=True)
    silver.build_all()
    # A separate target path keeps the project's real dbt manifest untouched.
    for args in (["seed"], ["run", "--full-refresh"]):
        warehouse.run_dbt(*args, "--target-path", str(root / "dbt_target"), data_dir=root)
    with duckdb.connect(str(root / "warehouse.duckdb"), read_only=True) as con:
        return con.execute("select * from feat_price_hourly order by zone, hour_start").df()


@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    root = tmp_path_factory.mktemp("pit")
    import elmorgon.silver as s

    saved = (s.BRONZE, s.SILVER, s.QUARANTINE)
    s.BRONZE, s.SILVER, s.QUARANTINE = root / "bronze", root / "silver", root / "quarantine"
    try:
        _write_bronze(root)
        before = _build(root)
        _write_bronze(root, shock=10.0)
        after = _build(root)
    finally:
        s.BRONZE, s.SILVER, s.QUARANTINE = saved
    return before, after


def test_features_for_a_day_ignore_that_days_prices(builds):
    before, after = builds
    feature_cols = [c for c in before.columns if c != "target_price"]
    day_before = before[before["delivery_date"] == pd.Timestamp(TARGET)][feature_cols]
    day_after = after[after["delivery_date"] == pd.Timestamp(TARGET)][feature_cols]
    assert len(day_before) == 4 * 24
    pd.testing.assert_frame_equal(
        day_before.reset_index(drop=True), day_after.reset_index(drop=True)
    )


def test_the_shock_is_visible_to_the_next_day(builds):
    """Guards the test above: the shocked prices do reach the following day's lags."""
    before, after = builds
    nxt = pd.Timestamp(TARGET + timedelta(days=1))
    lag_before = before.loc[before["delivery_date"] == nxt, "lag_1d"].to_numpy()
    lag_after = after.loc[after["delivery_date"] == nxt, "lag_1d"].to_numpy()
    assert (abs(lag_after) > abs(lag_before)).mean() > 0.9


def test_the_next_unpublished_day_gets_rows_with_forecast_weather(builds):
    before, _ = builds
    last = START + timedelta(days=DAYS - 1)
    live = before[before["target_price"].isna()]
    assert set(live["delivery_date"]) == {pd.Timestamp(last + timedelta(days=1))}
    assert len(live) == 4 * 24
    assert (live["weather_source"] == "forecast").all()
    assert live["temperature_c"].notna().all()
