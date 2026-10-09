"""Bronze layer: API responses stored exactly as received.

Prices land one file per zone per delivery day, archived weather one file per
month, and live weather forecasts one file per issue day. Re-running a step
only fetches what is missing or still changing, so every step is safe to repeat.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from elmorgon.config import (
    BRONZE,
    HISTORY_START,
    MARKET_TZ,
    WEATHER_POINTS,
    WEATHER_VARIABLES,
    ZONES,
)

log = logging.getLogger(__name__)

USER_AGENT = "elmorgon/0.1 (portfolio project; day-ahead price research)"
PRICES_URL = "https://www.elprisetjustnu.se/api/v1/prices"  # Nord Pool prices, no API key
WEATHER_ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def http_client() -> httpx.Client:
    transport = httpx.HTTPTransport(retries=3)
    return httpx.Client(transport=transport, timeout=60, headers={"User-Agent": USER_AGENT})


# Sources


def price_url(day: date, zone: str) -> str:
    return f"{PRICES_URL}/{day:%Y}/{day:%m-%d}_{zone}.json"


def fetch_price_day(client: httpx.Client, day: date, zone: str) -> list[dict] | None:
    """Raw price records for one delivery day, or None if not published yet."""
    response = client.get(price_url(day, zone))
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def fetch_weather(client: httpx.Client, url: str, **params: str) -> list[dict]:
    """Hourly weather for every configured point in one request (one dict per point)."""
    params |= {
        "latitude": ",".join(str(p.lat) for p in WEATHER_POINTS),
        "longitude": ",".join(str(p.lon) for p in WEATHER_POINTS),
        "hourly": ",".join(WEATHER_VARIABLES),
        "timezone": "UTC",
        "wind_speed_unit": "ms",
    }
    response = client.get(url, params=params)
    response.raise_for_status()
    return response.json()


# Files on disk


def price_path(zone: str, day: date) -> Path:
    return BRONZE / "prices" / zone / f"{day:%Y}" / f"{day.isoformat()}.json"


def weather_archive_path(month: date) -> Path:
    return BRONZE / "weather" / "archive" / f"{month:%Y-%m}.json"


def weather_forecast_path(issue_day: date) -> Path:
    return BRONZE / "weather" / "forecast" / f"{issue_day.isoformat()}.json"


def _write_json(path: Path, payload: object) -> None:
    # Write to a temp file first so a crash never leaves half a file behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def today_local() -> date:
    return datetime.now(MARKET_TZ).date()


def _days(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _months(start: date, end: date) -> Iterable[date]:
    month = start.replace(day=1)
    while month <= end:
        yield month
        month = (month + timedelta(days=32)).replace(day=1)


# Ingestion steps


def ingest_prices(
    start: date = HISTORY_START,
    end: date | None = None,
    refresh_days: int = 2,
    workers: int = 6,
) -> list[tuple[str, date]]:
    """Fetch price days that are missing, plus the most recent few, which can still change.

    `end` defaults to tomorrow: tomorrow's prices exist once the auction has
    published (about 13:00), and a 404 before then is expected, not an error.
    """
    end = end or today_local() + timedelta(days=1)
    refresh_from = today_local() - timedelta(days=refresh_days)
    todo = [
        (zone, day)
        for day in _days(start, end)
        for zone in ZONES
        if day >= refresh_from or not price_path(zone, day).exists()
    ]
    if not todo:
        return []

    with http_client() as client:

        def fetch(job: tuple[str, date]) -> tuple[str, date] | None:
            zone, day = job
            records = fetch_price_day(client, day, zone)
            if records is None:
                return None
            _write_json(price_path(zone, day), records)
            return job

        with ThreadPoolExecutor(max_workers=workers) as pool:
            written = [job for job in pool.map(fetch, todo) if job is not None]

    log.info("prices: %d requested, %d written", len(todo), len(written))
    return written


def ingest_weather_archive(start: date = HISTORY_START, end: date | None = None) -> list[date]:
    """Fetch archived weather by month; the current and previous month are always refreshed."""
    end = end or today_local()
    fresh_from = (end.replace(day=1) - timedelta(days=1)).replace(day=1)
    written = []
    with http_client() as client:
        for month in _months(start, end):
            path = weather_archive_path(month)
            if path.exists() and month < fresh_from:
                continue
            month_end = min((month + timedelta(days=32)).replace(day=1) - timedelta(days=1), end)
            payload = fetch_weather(
                client,
                WEATHER_ARCHIVE_URL,
                start_date=month.isoformat(),
                end_date=month_end.isoformat(),
            )
            _write_json(path, payload)
            written.append(month)
    log.info("weather archive: %d months written", len(written))
    return written


def ingest_weather_forecast(issue_day: date | None = None) -> Path:
    """Snapshot the live forecast once per issue day; later runs never overwrite it."""
    issue_day = issue_day or today_local()
    path = weather_forecast_path(issue_day)
    if not path.exists():
        with http_client() as client:
            payload = {
                "fetched_at": datetime.now(MARKET_TZ).isoformat(),
                "points": fetch_weather(client, WEATHER_FORECAST_URL, forecast_days="3"),
            }
        _write_json(path, payload)
    return path
