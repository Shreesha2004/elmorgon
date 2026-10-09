"""Shared fixtures: synthetic API payloads in the exact shape the sources return."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from elmorgon.config import MARKET_TZ, WEATHER_POINTS, WEATHER_VARIABLES


def price_records(day: date, resolution: int = 60, seed: int = 0, scale: float = 1.0) -> list[dict]:
    """One delivery day as elprisetjustnu.se returns it, DST included."""
    rng = np.random.default_rng(seed + day.toordinal())
    start = datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).astimezone(UTC)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=MARKET_TZ).astimezone(UTC)
    records, t = [], start
    while t < end:
        t_end = t + timedelta(minutes=resolution)
        price = round(
            float(scale * (0.5 + 0.4 * np.sin(t.hour / 24 * 6.28) + rng.normal(0, 0.1))), 5
        )
        records.append(
            {
                "SEK_per_kWh": price,
                "EUR_per_kWh": round(price / 11.2, 5),
                "EXR": 11.2,
                "time_start": t.astimezone(MARKET_TZ).isoformat(),
                "time_end": t_end.astimezone(MARKET_TZ).isoformat(),
            }
        )
        t = t_end
    return records


def weather_payload(start: date, end: date, seed: int = 0) -> list[dict]:
    """Open-Meteo's multi-point response: one dict per configured point."""
    rng = np.random.default_rng(seed)
    hours = []
    t = datetime(start.year, start.month, start.day)
    while t.date() <= end:
        hours.append(t.strftime("%Y-%m-%dT%H:%M"))
        t += timedelta(hours=1)
    return [
        {
            "latitude": p.lat,
            "longitude": p.lon,
            "hourly": {
                "time": hours,
                **{
                    v: [round(float(x), 2) for x in rng.normal(5, 3, len(hours))]
                    for v in WEATHER_VARIABLES
                },
            },
        }
        for p in WEATHER_POINTS
    ]


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """Point every layer at a temporary data directory."""
    from elmorgon import bronze, silver

    paths = {
        "BRONZE": tmp_path / "bronze",
        "SILVER": tmp_path / "silver",
        "QUARANTINE": tmp_path / "quarantine",
    }
    for module in (bronze, silver):
        for name, value in paths.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    return tmp_path
