"""EV and battery scheduling, their SEK valuation, the dashboard export and the CLI."""

import json
import math
from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from elmorgon import backtest, cli, forecast, report, schedule, valuation, warehouse
from elmorgon.config import MARKET_TZ
from elmorgon.schedule import EV, Battery

DAYS = [date(2025, 9, 29), date(2025, 9, 30), date(2025, 10, 1), date(2025, 10, 2)]


# Scheduling


@pytest.mark.parametrize("seed", range(20))
def test_greedy_ev_plan_matches_the_linear_program(seed):
    rng = np.random.default_rng(seed)
    n, step = (96, 0.25) if seed % 2 else (24, 1.0)
    prices = rng.normal(0.6, 0.5, n)
    hours = (np.arange(n) * step).astype(int)
    available = np.isin(hours, EV().home_hours)
    greedy = schedule.ev_plan(prices, available, step)
    lp = schedule.ev_plan_lp(prices, available, step)
    assert prices @ greedy == pytest.approx(prices @ lp, abs=1e-9)


def test_ev_plan_respects_power_home_hours_and_energy():
    prices = np.random.default_rng(42).normal(0.6, 0.5, 96)
    available = np.isin(np.arange(96) // 4, EV().home_hours)
    plan = schedule.ev_plan(prices, available, 0.25)
    assert plan.sum() == pytest.approx(EV().energy_kwh)
    assert plan.max() <= EV().power_kw * 0.25 + 1e-9
    assert plan[~available].sum() == 0


def test_ev_plan_picks_the_cheapest_hours():
    prices = np.full(24, 1.0)
    prices[3] = 0.1  # cheap night hour at home
    prices[12] = -1.0  # cheapest, but the car is at work
    plan = schedule.ev_plan(prices, np.isin(np.arange(24), EV().home_hours), 1.0)
    assert plan[12] == 0
    assert plan[3] == pytest.approx(min(EV().power_kw, EV().energy_kwh))


def test_not_enough_time_to_charge_is_an_error():
    with pytest.raises(ValueError):
        schedule.ev_plan(np.ones(24), np.zeros(24, dtype=bool), 1.0)


def test_charging_on_arrival_starts_at_six_pm():
    plan = schedule.ev_on_arrival(np.arange(24), 1.0)
    assert plan[:18].sum() == 0
    assert plan[18] == pytest.approx(min(EV().power_kw, EV().energy_kwh))
    assert plan.sum() == pytest.approx(EV().energy_kwh)


def test_battery_respects_physics():
    prices = np.r_[np.full(8, 0.2), np.full(8, 1.5), np.full(8, 0.4)]
    bat = Battery()
    c, d = schedule.battery_plan(prices, 1.0)
    eta = np.sqrt(bat.round_trip)
    soc = bat.start_soc_kwh + np.cumsum(eta * c - d / eta)
    assert soc.min() >= -1e-6 and soc.max() <= bat.capacity_kwh + 1e-6
    assert soc[-1] == pytest.approx(bat.start_soc_kwh, abs=1e-6)
    assert max(c.max(), d.max()) <= bat.power_kw + 1e-9
    assert prices @ (d - c) - bat.wear_sek_per_kwh * d.sum() > 0


def test_battery_sits_still_when_prices_are_flat():
    c, d = schedule.battery_plan(np.full(24, 0.7), 1.0)
    assert c.sum() == pytest.approx(0, abs=1e-9)
    assert d.sum() == pytest.approx(0, abs=1e-9)


def test_hourly_plan_spreads_evenly_over_quarter_hours():
    hourly = np.array([4.0, 0.0, 2.0])
    spread = schedule.spread_to_intervals(hourly, np.array([4, 4, 1]))
    assert spread.tolist() == [1, 1, 1, 1, 0, 0, 0, 0, 2]
    assert spread.sum() == hourly.sum()


# Valuation and backtest scoring


def _market(day: date, zone: str, resolution: int, rng) -> pd.DataFrame:
    start = pd.Timestamp(day, tz=MARKET_TZ).tz_convert("UTC")
    n = 24 * 60 // resolution
    starts = start + pd.to_timedelta(np.arange(n) * resolution, unit="min")
    hours = starts.tz_convert(MARKET_TZ).hour
    price = 0.6 + 0.5 * np.sin((hours - 6) / 24 * 2 * math.pi) + rng.normal(0, 0.05, n)
    return pd.DataFrame(
        {
            "zone": zone,
            "delivery_date": day,
            "interval_start": starts,
            "resolution_minutes": resolution,
            "price_sek_kwh": price,
        }
    )


@pytest.fixture
def market_and_predictions(monkeypatch):
    """Two hourly days then two quarter-hour days, as across the Oct 2025 switch."""
    rng = np.random.default_rng(3)
    market = pd.concat(
        [
            _market(d, z, 60 if d < date(2025, 10, 1) else 15, rng)
            for d in DAYS
            for z in ("SE3", "SE4")
        ],
        ignore_index=True,
    )
    market["hour_start"] = market["interval_start"].dt.floor("h")
    hourly = market.groupby(["zone", "delivery_date", "hour_start"], as_index=False)[
        "price_sek_kwh"
    ].mean()
    hourly["hour_local"] = hourly["hour_start"].dt.tz_convert(MARKET_TZ).dt.hour
    rows = []
    for model, noise in (("gbm", 0.05), ("naive_day", 0.3)):
        p = hourly.rename(columns={"price_sek_kwh": "target_price"}).copy()
        p["q50"] = p["target_price"] + rng.normal(0, noise, len(p))
        p["q10"], p["q90"] = p["q50"] - 0.2, p["q50"] + 0.2
        p["model"] = model
        p["fold"] = date(2025, 9, 1)
        rows.append(p)
    monkeypatch.setattr(
        warehouse, "load_interval_prices", lambda a, b: market.drop(columns="hour_start").copy()
    )
    return pd.concat(rows, ignore_index=True)


def test_valuation_orders_strategies_sensibly(market_and_predictions, tmp_path, monkeypatch):
    monkeypatch.setattr(valuation, "DAILY", tmp_path / "daily.parquet")
    monkeypatch.setattr(valuation, "SUMMARY", tmp_path / "summary.json")
    daily = valuation.run(market_and_predictions)
    assert len(daily) == len(DAYS) * 2
    # Perfect foresight is a lower bound on cost and an upper bound on profit.
    assert (daily["ev_oracle"] <= daily["ev_elmorgon"] + 1e-9).all()
    assert (daily["ev_oracle"] <= daily["ev_on_arrival"] + 1e-9).all()
    assert (daily["bat_oracle"] >= daily["bat_elmorgon"] - 1e-9).all()

    summary = valuation.summarise(daily)
    ev = summary["overall"]["ev"]
    assert ev["sek_per_kwh"]["oracle"] <= ev["sek_per_kwh"]["elmorgon"]
    assert ev["share_of_possible_saving"]["elmorgon"] > ev["share_of_possible_saving"]["naive"]
    assert json.loads((tmp_path / "summary.json").read_text())["by_zone"].keys() == {"SE3", "SE4"}


def test_backtest_score_writes_every_view(market_and_predictions, tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "METRICS", tmp_path / "metrics.json")
    preds = market_and_predictions.copy()
    preds["q10_raw"], preds["q90_raw"] = preds["q10"], preds["q90"]
    result = backtest.score(preds)
    assert result["overall"]["gbm"]["mae"] < result["overall"]["naive_day"]["mae"]
    assert "coverage_80_uncalibrated" in result["overall"]["gbm"]
    assert set(result["by_zone"]) == {"SE3", "SE4"}
    assert result["monthly_mae"][0]["month"] == "2025-09"


# Dashboard export, warehouse and CLI


def test_clean_makes_values_json_safe():
    cleaned = report._clean(
        {
            "a": float("nan"),
            "b": [pd.Timestamp("2025-01-01", tz="UTC")],
            "c": np.float64(1.234567891),
            "d": date(2025, 1, 2),
            "e": np.bool_(True),
        }
    )
    assert cleaned == {
        "a": None,
        "b": ["2025-01-01T00:00:00+00:00"],
        "c": 1.23457,
        "d": "2025-01-02",
        "e": True,
    }
    json.dumps(cleaned)


def test_track_record_compares_issued_forecasts_with_actuals(monkeypatch):
    tr = pd.DataFrame(
        {
            "delivery_date": [date(2025, 1, 2)] * 2 + [date(2025, 1, 3)] * 2,
            "zone": ["SE3"] * 4,
            "q10": [0, 0, 0, 0],
            "q50": [1.0, 1.0, 1.0, 1.0],
            "q90": [2, 2, 2, 2],
            "naive_day": [0.0, 0.0, 1.0, 1.0],
            "actual": [1.0, 1.5, 3.0, 1.0],
        }
    )
    monkeypatch.setattr(forecast, "track_record", lambda: tr)
    out = report.track_record()
    assert out["summary"]["days"] == 2
    assert out["summary"]["days_beating_naive"] == 1
    assert out["summary"]["coverage_80"] == pytest.approx(0.75)


def test_warehouse_query_reads_the_duckdb_file(tmp_path, monkeypatch):
    db = tmp_path / "w.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute(
            "create table fct_price_interval as select 'SE3' as zone, "
            "date '2025-01-01' as delivery_date, timestamptz '2025-01-01 00:00:00+00' "
            "as interval_start, 60 as resolution_minutes, 0.5 as price_sek_kwh"
        )
    monkeypatch.setattr(warehouse, "WAREHOUSE", db)
    frame = warehouse.load_interval_prices(date(2025, 1, 1), date(2025, 1, 1))
    assert frame["delivery_date"].iloc[0] == date(2025, 1, 1)
    assert str(frame["interval_start"].dt.tz) == "UTC"


def test_failed_dbt_command_raises_with_its_output():
    with pytest.raises(RuntimeError, match="dbt no-such-command failed"):
        warehouse.run_dbt("no-such-command")


def test_cli_daily_runs_every_step_in_order(monkeypatch):
    calls = []
    for name in ("cmd_ingest", "cmd_build", "cmd_forecast", "cmd_site"):
        monkeypatch.setattr(cli, name, lambda _args, n=name: calls.append(n))
    cli.main(["daily"])
    assert calls == ["cmd_ingest", "cmd_build", "cmd_forecast", "cmd_site"]


def test_cli_rejects_unknown_commands():
    with pytest.raises(SystemExit):
        cli.main(["teleport"])
