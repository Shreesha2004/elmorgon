"""Constants and paths shared by every stage of the pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ELMORGON_DATA_DIR", ROOT / "data"))

BRONZE = DATA_DIR / "bronze"
SILVER = DATA_DIR / "silver"
QUARANTINE = DATA_DIR / "quarantine"
WAREHOUSE = DATA_DIR / "warehouse.duckdb"
FORECASTS = DATA_DIR / "forecasts"
# Live forecasts sit outside data/ so they are versioned with the code: once
# published, the history shows they were never edited after the prices came in.
ISSUED = Path(os.environ.get("ELMORGON_ISSUED_DIR", ROOT / "forecasts" / "issued"))
DBT_DIR = ROOT / "transform"
SITE_DIR = ROOT / "site"

MARKET_TZ = ZoneInfo("Europe/Stockholm")
ZONES = ("SE1", "SE2", "SE3", "SE4")

# First delivery day the price API serves.
HISTORY_START = date(2022, 11, 1)
# First month the backtest predicts; earlier data is training-only.
BACKTEST_START = date(2024, 1, 1)
# Hour of day D (local time) when the forecast for D+1 is issued.
ISSUE_HOUR = 10


@dataclass(frozen=True)
class WeatherPoint:
    name: str
    zone: str
    lat: float
    lon: float


# Two points per zone: one population centre, one that tracks wind or hydro conditions.
WEATHER_POINTS = (
    WeatherPoint("Lulea", "SE1", 65.58, 22.15),
    WeatherPoint("Kiruna", "SE1", 67.86, 20.23),
    WeatherPoint("Sundsvall", "SE2", 62.39, 17.31),
    WeatherPoint("Ostersund", "SE2", 63.18, 14.64),
    WeatherPoint("Stockholm", "SE3", 59.33, 18.07),
    WeatherPoint("Goteborg", "SE3", 57.71, 11.97),
    WeatherPoint("Malmo", "SE4", 55.60, 13.00),
    WeatherPoint("Kalmar", "SE4", 56.66, 16.36),
)

WEATHER_VARIABLES = ("temperature_2m", "wind_speed_100m", "shortwave_radiation", "cloud_cover")
