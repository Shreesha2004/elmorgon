"""Export everything the dashboard shows into site/data/dashboard.json."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import pandas as pd

from elmorgon import backtest, forecast, valuation, warehouse
from elmorgon.config import DBT_DIR, MARKET_TZ, QUARANTINE, SITE_DIR, ZONES

OUT = SITE_DIR / "data" / "dashboard.json"


def _clean(value):
    """JSON-safe: NaN/inf become null, timestamps become ISO strings."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 5)
    if isinstance(value, pd.Timestamp | datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return _clean(value.item())
    return value


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def tomorrow() -> dict | None:
    """The latest issued forecast, hour by hour for each zone."""
    issued = forecast.load_issued()
    if issued.empty:
        return None
    target = issued["delivery_date"].max()
    day = issued[issued["delivery_date"] == target].sort_values(["zone", "hour_start"])
    return {
        "date": target,
        "issued_at": day["issued_at"].iloc[0],
        "model_version": day["model_version"].iloc[0],
        "zones": {
            z: [
                {
                    "hour": int(r.hour_local),
                    "start": r.hour_start,
                    "q10": r.q10,
                    "q50": r.q50,
                    "q90": r.q90,
                    "naive": r.naive_day,
                }
                for r in g.itertuples()
            ]
            for z, g in day.groupby("zone")
        },
    }


def latest_actual() -> dict:
    """The most recent published delivery day, at market resolution, in local time."""
    last = pd.Timestamp(
        warehouse.query("select max(delivery_date) as d from fct_price_interval")["d"].iloc[0]
    ).date()
    rows = warehouse.load_interval_prices(last, last)
    rows["local"] = rows["interval_start"].dt.tz_convert(MARKET_TZ)
    return {
        "date": last,
        "resolution_minutes": int(rows["resolution_minutes"].iloc[0]),
        "zones": {
            z: [{"start": r.local, "price": r.price_sek_kwh} for r in g.itertuples()]
            for z, g in rows.groupby("zone")
        },
    }


def recent_days(n: int = 120) -> dict:
    """Daily price statistics for the last n days, per zone."""
    stats = warehouse.query(
        """
        select delivery_date, zone, mean_price, min_price, max_price, intraday_spread,
               negative_intervals
        from mart_daily_zone_stats
        where delivery_date > (select max(delivery_date) from mart_daily_zone_stats) - ?
        order by delivery_date
        """,
        [n],
    )
    stats["delivery_date"] = pd.to_datetime(stats["delivery_date"]).dt.date
    return {z: g.drop(columns="zone").to_dict(orient="records") for z, g in stats.groupby("zone")}


def issued_log() -> list[dict]:
    """Every issued forecast, newest first, scored or still waiting for its prices."""
    issued = forecast.load_issued()
    if issued.empty:
        return []
    log = issued.groupby("delivery_date", as_index=False).agg(
        issued_at=("issued_at", "first"), model_version=("model_version", "first")
    )
    return log.sort_values("delivery_date", ascending=False).to_dict(orient="records")


def track_record() -> dict:
    """How the live forecasts did against the naive baseline, per day and overall."""
    tr = forecast.track_record()
    if tr.empty:
        return {"days": [], "summary": None, "issued": issued_log()}
    tr["err_elmorgon"] = (tr["q50"] - tr["actual"]).abs()
    tr["err_naive"] = (tr["naive_day"] - tr["actual"]).abs()
    tr["inside"] = (tr["actual"] >= tr["q10"]) & (tr["actual"] <= tr["q90"])
    days = (
        tr.groupby(["delivery_date", "zone"])
        .agg(
            mae_elmorgon=("err_elmorgon", "mean"),
            mae_naive=("err_naive", "mean"),
            coverage=("inside", "mean"),
        )
        .reset_index()
    )
    by_day = days.groupby("delivery_date")[["mae_elmorgon", "mae_naive"]].mean()
    return {
        "days": days.to_dict(orient="records"),
        "issued": issued_log(),
        "summary": {
            "days": int(by_day.shape[0]),
            "mae_elmorgon": float(tr["err_elmorgon"].mean()),
            "mae_naive": float(tr["err_naive"].mean()),
            "days_beating_naive": int((by_day["mae_elmorgon"] < by_day["mae_naive"]).sum()),
            "coverage_80": float(tr["inside"].mean()),
        },
    }


def pipeline_health() -> dict:
    """Data coverage, quarantined days and dbt test results."""
    counts = warehouse.query(
        """
        select count(distinct delivery_date) as days, count(*) as intervals,
               min(delivery_date) as first_day, max(delivery_date) as last_day
        from fct_price_interval
        """
    ).iloc[0]
    run_results = _read_json(DBT_DIR / "target" / "run_results.json") or {"results": []}
    statuses = [r["status"] for r in run_results["results"] if r["unique_id"].startswith("test.")]
    return {
        "first_day": pd.Timestamp(counts["first_day"]).date(),
        "last_day": pd.Timestamp(counts["last_day"]).date(),
        "days": int(counts["days"]),
        "intervals": int(counts["intervals"]),
        "zones": list(ZONES),
        "quarantined_days": len(list((QUARANTINE / "prices").glob("*.json")))
        if (QUARANTINE / "prices").exists()
        else 0,
        "data_tests": {"passed": statuses.count("pass"), "total": len(statuses)},
    }


def export() -> dict:
    """Write everything above to OUT."""
    data = {
        "generated_at": datetime.now(UTC),
        "tomorrow": tomorrow(),
        "latest_actual": latest_actual(),
        "recent_days": recent_days(),
        "backtest": _read_json(backtest.METRICS),
        "value": _read_json(valuation.SUMMARY),
        "track_record": track_record(),
        "pipeline": pipeline_health(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(_clean(data), ensure_ascii=False), encoding="utf-8")
    return data
