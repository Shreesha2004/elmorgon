"""Silver layer: validated, typed Parquet built from bronze.

Each price day is checked as a whole. A day that fails any rule is written to
quarantine with the reason and kept out of silver, so one bad payload can
never reach the warehouse silently.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from elmorgon.config import BRONZE, MARKET_TZ, QUARANTINE, SILVER, WEATHER_POINTS, WEATHER_VARIABLES

log = logging.getLogger(__name__)

MAX_ABS_PRICE = 100.0  # SEK/kWh; the record so far is ~25. Negative prices are valid.


class ValidationError(ValueError):
    """A bronze payload that must not reach silver."""


@dataclass(frozen=True)
class DayCheck:
    zone: str
    day: date
    resolution_minutes: int
    intervals: int


def expected_intervals(day: date, resolution_minutes: int) -> int:
    """Intervals in a local delivery day: 23, 24 or 25 hours around DST changes."""
    nxt = day + timedelta(days=1)
    # Subtract in UTC: Python ignores the offset change when both ends share a tzinfo.
    start = datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).astimezone(UTC)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=MARKET_TZ).astimezone(UTC)
    minutes = (end - start).total_seconds() / 60
    return int(minutes // resolution_minutes)


def parse_price_day(records: list[dict], zone: str, day: date) -> pd.DataFrame:
    """Turn one day's API records into rows, or raise ValidationError."""
    if not records:
        raise ValidationError("empty payload")
    try:
        frame = pd.DataFrame(
            {
                "interval_start": pd.to_datetime([r["time_start"] for r in records], utc=True),
                "price_sek_kwh": pd.to_numeric([r["SEK_per_kWh"] for r in records]),
                "price_eur_kwh": pd.to_numeric([r["EUR_per_kWh"] for r in records]),
                "eur_sek_rate": pd.to_numeric([r["EXR"] for r in records]),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"malformed record: {exc}") from exc

    if frame["interval_start"].duplicated().any():
        raise ValidationError("duplicate interval starts")
    frame = frame.sort_values("interval_start", ignore_index=True)

    # Resolution comes from the start times. The source's `time_end` is ignored:
    # on autumn DST days it carries the wrong offset for the interval before the
    # repeated hour (e.g. 02:00+02:00 -> 03:00+01:00, a 2-hour "interval").
    steps = frame["interval_start"].diff().dropna().dt.total_seconds() / 60
    resolution = int(steps.mode().iloc[0]) if len(steps) else 0
    if resolution not in (15, 60):
        raise ValidationError(f"interval length {resolution} min")
    frame.insert(1, "interval_end", frame["interval_start"] + pd.Timedelta(minutes=resolution))

    first_local = frame["interval_start"].iloc[0].tz_convert(MARKET_TZ)
    if first_local.date() != day or (first_local.hour, first_local.minute) != (0, 0):
        raise ValidationError(f"day starts at {first_local.isoformat()}")

    if (steps != resolution).any():
        raise ValidationError("gap or overlap between intervals")

    expected = expected_intervals(day, resolution)
    if len(frame) != expected:
        raise ValidationError(f"{len(frame)} intervals, expected {expected}")

    if frame["price_sek_kwh"].isna().any():
        raise ValidationError("missing price")
    if (frame["price_sek_kwh"].abs() > MAX_ABS_PRICE).any():
        raise ValidationError(f"price outside +/-{MAX_ABS_PRICE} SEK/kWh")

    frame.insert(0, "zone", zone)
    frame.insert(1, "delivery_date", day)
    frame["resolution_minutes"] = resolution
    return frame


def _quarantine(kind: str, name: str, source: Path, reason: str) -> None:
    target = QUARANTINE / kind / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"source": str(source), "reason": reason}, indent=2), encoding="utf-8"
    )


def _stale(target: Path, sources: list[Path]) -> bool:
    """True if the target is missing or older than any of its source files."""
    if not target.exists():
        return True
    built = target.stat().st_mtime
    return any(s.stat().st_mtime > built for s in sources)


def build_prices() -> dict[str, int]:
    """Rebuild every zone-month whose bronze files changed since it was last written."""
    stats = {"months_built": 0, "days_ok": 0, "days_quarantined": 0}
    root = BRONZE / "prices"
    if not root.exists():
        return stats

    groups: dict[tuple[str, str], list[Path]] = {}
    for path in sorted(root.glob("*/*/*.json")):
        zone = path.parent.parent.name
        groups.setdefault((zone, path.stem[:7]), []).append(path)

    for (zone, month), paths in groups.items():
        target = SILVER / "prices" / f"zone={zone}" / f"{month}.parquet"
        if not _stale(target, paths):
            continue
        frames = []
        for path in paths:
            day = date.fromisoformat(path.stem)
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
                frames.append(parse_price_day(records, zone, day))
                stats["days_ok"] += 1
                (QUARANTINE / "prices" / f"{zone}_{day}.json").unlink(missing_ok=True)
            except (ValidationError, json.JSONDecodeError) as exc:
                _quarantine("prices", f"{zone}_{day}", path, str(exc))
                stats["days_quarantined"] += 1
                log.warning("quarantined %s %s: %s", zone, day, exc)
        if frames:
            target.parent.mkdir(parents=True, exist_ok=True)
            pd.concat(frames, ignore_index=True).drop(columns="zone").to_parquet(
                target, index=False
            )
            stats["months_built"] += 1
    return stats


def parse_weather(points_payload: list[dict]) -> pd.DataFrame:
    """Flatten Open-Meteo's one-dict-per-point response into long rows."""
    if len(points_payload) != len(WEATHER_POINTS):
        raise ValidationError(
            f"{len(points_payload)} weather points, expected {len(WEATHER_POINTS)}"
        )
    frames = []
    for point, payload in zip(WEATHER_POINTS, points_payload, strict=True):
        hourly = payload.get("hourly") or {}
        missing = [v for v in ("time", *WEATHER_VARIABLES) if v not in hourly]
        if missing:
            raise ValidationError(f"{point.name}: missing {missing}")
        frame = pd.DataFrame({v: hourly[v] for v in WEATHER_VARIABLES})
        frame.insert(0, "valid_at", pd.to_datetime(hourly["time"], utc=True))
        frame.insert(0, "zone", point.zone)
        frame.insert(0, "point", point.name)
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    for v in WEATHER_VARIABLES:
        out[v] = pd.to_numeric(out[v], errors="coerce").astype("float64")
    return out


def build_weather_archive() -> int:
    """One Parquet per archived month."""
    built = 0
    for path in sorted((BRONZE / "weather" / "archive").glob("*.json")):
        target = SILVER / "weather_archive" / f"{path.stem}.parquet"
        if not _stale(target, [path]):
            continue
        try:
            frame = parse_weather(json.loads(path.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError) as exc:
            _quarantine("weather", path.stem, path, str(exc))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False)
        built += 1
    return built


def build_weather_forecasts() -> int:
    """One Parquet per issue day, tagged with when the forecast was fetched."""
    built = 0
    for path in sorted((BRONZE / "weather" / "forecast").glob("*.json")):
        target = SILVER / "weather_forecast" / f"{path.stem}.parquet"
        if not _stale(target, [path]):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            frame = parse_weather(payload["points"])
        except (ValidationError, KeyError) as exc:
            _quarantine("weather_forecast", path.stem, path, str(exc))
            continue
        frame.insert(0, "issue_date", date.fromisoformat(path.stem))
        frame.insert(1, "fetched_at", pd.Timestamp(payload["fetched_at"]).tz_convert("UTC"))
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False)
        built += 1
    return built


def build_all() -> dict[str, int]:
    """Rebuild everything in silver that is out of date."""
    stats = build_prices()
    stats["weather_months_built"] = build_weather_archive()
    stats["weather_forecasts_built"] = build_weather_forecasts()
    return stats
