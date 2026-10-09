"""What each forecast is worth in SEK, over every day of the backtest.

For each zone and delivery day, plans are made from a forecast and costed at
the prices that actually cleared:

- EV: Elmorgon's forecast, "same as yesterday", charging on arrival at 18:00, and
  perfect foresight.
- Battery: Elmorgon's forecast, "same as yesterday", and perfect foresight.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from elmorgon import schedule, warehouse
from elmorgon.config import FORECASTS, MARKET_TZ

log = logging.getLogger(__name__)

DAILY = FORECASTS / "schedule_daily.parquet"
SUMMARY = FORECASTS / "schedule_summary.json"


# Cost (EV) and profit (battery) of every strategy for one zone and day.
def _day_values(
    hours: pd.DataFrame, intervals: pd.DataFrame, ev: schedule.EV, bat: schedule.Battery
) -> dict[str, float]:
    price = intervals["price_sek_kwh"].to_numpy()
    step_h = intervals["resolution_minutes"].iloc[0] / 60
    local_hour = intervals["local_hour"].to_numpy()
    per_hour = intervals.groupby("hour_start", sort=True).size().to_numpy()
    home_hourly = hours["hour_local"].isin(ev.home_hours).to_numpy()
    home_interval = np.isin(local_hour, ev.home_hours)

    def ev_cost(plan: np.ndarray) -> float:
        return float(price @ plan)

    def bat_profit(charge: np.ndarray, discharge: np.ndarray) -> float:
        return float(price @ (discharge - charge) - bat.wear_sek_per_kwh * discharge.sum())

    out: dict[str, float] = {}
    for name, col in (("elmorgon", "gbm"), ("naive", "naive_day")):
        forecast = hours[col].to_numpy()
        plan = schedule.ev_plan(forecast, home_hourly, 1.0, ev)
        out[f"ev_{name}"] = ev_cost(schedule.spread_to_intervals(plan, per_hour))
        c, d = schedule.battery_plan(forecast, 1.0, bat)
        out[f"bat_{name}"] = bat_profit(
            schedule.spread_to_intervals(c, per_hour), schedule.spread_to_intervals(d, per_hour)
        )
    out["ev_on_arrival"] = ev_cost(schedule.ev_on_arrival(local_hour, step_h, ev))
    out["ev_oracle"] = ev_cost(schedule.ev_plan(price, home_interval, step_h, ev))
    c, d = schedule.battery_plan(price, step_h, bat)
    out["bat_oracle"] = bat_profit(c, d)
    return out


def run(
    predictions: pd.DataFrame,
    ev: schedule.EV = schedule.EV(),
    bat: schedule.Battery = schedule.Battery(),
) -> pd.DataFrame:
    """Value every zone-day of the backtest; written to DAILY."""
    wide = (
        predictions[predictions["model"].isin(["gbm", "naive_day"])]
        .pivot_table(
            index=["zone", "delivery_date", "hour_start", "hour_local"],
            columns="model",
            values="q50",
        )
        .reset_index()
    )
    first, last = wide["delivery_date"].min(), wide["delivery_date"].max()
    actual = warehouse.load_interval_prices(first, last)
    actual["hour_start"] = actual["interval_start"].dt.floor("h")
    actual["local_hour"] = actual["interval_start"].dt.tz_convert(MARKET_TZ).dt.hour

    rows = []
    for (zone, day), hours in wide.groupby(["zone", "delivery_date"], sort=True):
        intervals = actual[(actual["zone"] == zone) & (actual["delivery_date"] == day)]
        hours = hours.sort_values("hour_start")
        if intervals.empty or set(hours["hour_start"]) != set(intervals["hour_start"]):
            log.warning("skipping %s %s: forecast and market hours differ", zone, day)
            continue
        rows.append(
            {
                "zone": zone,
                "delivery_date": day,
                **_day_values(hours, intervals.sort_values("interval_start"), ev, bat),
            }
        )
    daily = pd.DataFrame(rows)
    DAILY.parent.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(DAILY, index=False)
    return daily


def summarise(daily: pd.DataFrame, ev: schedule.EV = schedule.EV()) -> dict:
    """Yearly SEK figures and shares of the perfect-foresight result; written to SUMMARY."""

    def block(d: pd.DataFrame) -> dict:
        m = d.mean(numeric_only=True)
        ev_gap = m["ev_on_arrival"] - m["ev_oracle"]
        return {
            "days": int(len(d)),
            "ev": {
                "sek_per_kwh": {
                    k: float(m[f"ev_{k}"] / ev.energy_kwh)
                    for k in ("on_arrival", "naive", "elmorgon", "oracle")
                },
                "saving_sek_per_year": {
                    k: float((m["ev_on_arrival"] - m[f"ev_{k}"]) * 365)
                    for k in ("naive", "elmorgon", "oracle")
                },
                "share_of_possible_saving": {
                    k: float((m["ev_on_arrival"] - m[f"ev_{k}"]) / ev_gap) if ev_gap else 0.0
                    for k in ("naive", "elmorgon")
                },
            },
            "battery": {
                "profit_sek_per_year": {
                    k: float(m[f"bat_{k}"] * 365) for k in ("naive", "elmorgon", "oracle")
                },
                "share_of_possible_profit": {
                    k: float(m[f"bat_{k}"] / m["bat_oracle"]) if m["bat_oracle"] else 0.0
                    for k in ("naive", "elmorgon")
                },
            },
        }

    summary = {
        "assumptions": {
            "ev": {
                "energy_kwh_per_day": ev.energy_kwh,
                "charger_kw": ev.power_kw,
                "home": "00:00-07:00 and 18:00-24:00",
                "on_arrival_starts": "18:00",
            },
            "battery": vars(schedule.Battery()),
            "prices": "Spot price only, excluding grid fees, energy tax and VAT.",
        },
        "overall": block(daily),
        "by_zone": {z: block(g) for z, g in daily.groupby("zone")},
        "period": {
            "first_day": str(daily["delivery_date"].min()),
            "last_day": str(daily["delivery_date"].max()),
        },
    }
    SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
