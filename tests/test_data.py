"""Bronze ingestion (against mocked APIs) and silver validation."""

import json
from datetime import date

import httpx
import pandas as pd
import pytest
import respx

from elmorgon import bronze, silver
from elmorgon.silver import ValidationError, expected_intervals, parse_price_day
from tests.conftest import price_records, weather_payload, write_json

SPRING = date(2025, 3, 30)  # 23 hours
AUTUMN = date(2025, 10, 26)  # 25 hours, and the market is 15-minute by then
PLAIN = date(2025, 6, 10)


# Bronze


def test_price_url_matches_the_api_layout():
    assert bronze.price_url(date(2024, 1, 5), "SE4") == (
        "https://www.elprisetjustnu.se/api/v1/prices/2024/01-05_SE4.json"
    )


@respx.mock
def test_unpublished_day_returns_none():
    respx.get(bronze.price_url(date(2030, 1, 1), "SE3")).respond(404)
    with httpx.Client() as client:
        assert bronze.fetch_price_day(client, date(2030, 1, 1), "SE3") is None


@respx.mock
def test_weather_request_covers_every_point():
    route = respx.get(bronze.WEATHER_ARCHIVE_URL).respond(
        json=weather_payload(date(2025, 1, 1), date(2025, 1, 1))
    )
    with httpx.Client() as client:
        bronze.fetch_weather(client, bronze.WEATHER_ARCHIVE_URL, start_date="2025-01-01")
    params = route.calls.last.request.url.params
    assert len(params["latitude"].split(",")) == 8
    assert params["timezone"] == "UTC"
    assert params["start_date"] == "2025-01-01"


@respx.mock
def test_ingest_fetches_only_missing_or_recent_days(data_dirs, monkeypatch):
    today = date(2025, 6, 10)
    monkeypatch.setattr(bronze, "today_local", lambda: today)
    monkeypatch.setattr(bronze, "ZONES", ("SE3",))
    old = date(2025, 6, 1)
    path = bronze.price_path("SE3", old)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(price_records(old)))

    calls = []

    def respond(request):
        day = date.fromisoformat("2025-" + request.url.path.split("/")[-1][:5])
        calls.append(day)
        if day > today:  # tomorrow is not published yet
            return httpx.Response(404)
        return httpx.Response(200, json=price_records(day))

    respx.get(url__startswith=bronze.PRICES_URL).mock(side_effect=respond)
    written = bronze.ingest_prices(start=old, workers=1)

    assert old not in calls  # already on disk and old enough to be final
    assert date(2025, 6, 11) in calls  # tomorrow was tried
    assert ("SE3", date(2025, 6, 11)) not in written  # but it was not published
    assert ("SE3", today) in written


@respx.mock
def test_weather_forecast_snapshot_is_never_overwritten(data_dirs):
    route = respx.get(bronze.WEATHER_FORECAST_URL).respond(
        json=weather_payload(date(2025, 6, 10), date(2025, 6, 12))
    )
    first = bronze.ingest_weather_forecast(date(2025, 6, 10))
    before = first.read_text()
    bronze.ingest_weather_forecast(date(2025, 6, 10))
    assert route.call_count == 1
    assert first.read_text() == before


@respx.mock
def test_weather_archive_skips_finished_months(data_dirs):
    route = respx.get(bronze.WEATHER_ARCHIVE_URL).respond(
        json=weather_payload(date(2025, 1, 1), date(2025, 1, 1))
    )
    end = date(2025, 4, 15)
    assert len(bronze.ingest_weather_archive(date(2025, 1, 1), end)) == 4
    # Second run: only March (previous month) and April (current month) refresh.
    assert bronze.ingest_weather_archive(date(2025, 1, 1), end) == [
        date(2025, 3, 1),
        date(2025, 4, 1),
    ]
    assert route.call_count == 6


# Silver: price days


@pytest.mark.parametrize(
    ("day", "resolution", "expected"),
    [
        (PLAIN, 60, 24),
        (SPRING, 60, 23),
        (date(2024, 10, 27), 60, 25),
        (AUTUMN, 15, 100),
        (date(2026, 3, 29), 15, 92),
    ],
)
def test_expected_intervals_follow_local_day_length(day, resolution, expected):
    assert expected_intervals(day, resolution) == expected


def test_parses_a_plain_hourly_day():
    frame = parse_price_day(price_records(PLAIN), "SE3", PLAIN)
    assert len(frame) == 24
    assert (frame["resolution_minutes"] == 60).all()
    assert frame["interval_start"].dt.tz is not None
    assert (frame["interval_end"] - frame["interval_start"]).eq(pd.Timedelta(hours=1)).all()


def test_parses_quarter_hour_days():
    day = date(2026, 1, 15)
    frame = parse_price_day(price_records(day, resolution=15), "SE4", day)
    assert len(frame) == 96
    assert (frame["resolution_minutes"] == 15).all()


def test_spring_dst_day_has_23_hours():
    assert len(parse_price_day(price_records(SPRING), "SE1", SPRING)) == 23


def test_autumn_dst_source_defect_is_repaired():
    """The API gives one interval a 2-hour time_end on the autumn change; starts are right."""
    records = price_records(AUTUMN, resolution=15)
    first_0200 = next(
        i for i, r in enumerate(records) if r["time_start"].endswith("02:45:00+02:00")
    )
    records[first_0200]["time_end"] = "2025-10-26T04:00:00+01:00"  # what the source sends
    frame = parse_price_day(records, "SE3", AUTUMN)
    assert len(frame) == 100
    assert (frame["interval_end"] - frame["interval_start"]).eq(pd.Timedelta(minutes=15)).all()


def test_negative_prices_are_valid():
    records = price_records(PLAIN)
    records[3]["SEK_per_kWh"] = -0.42
    assert parse_price_day(records, "SE2", PLAIN)["price_sek_kwh"].min() == -0.42


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.pop(5), "gap"),
        (lambda r: r.append(dict(r[-1])), "duplicate"),
        (lambda r: r[2].update(SEK_per_kWh=250.0), "price outside"),
        (lambda r: r[2].pop("SEK_per_kWh"), "malformed"),
        (lambda r: r.clear(), "empty"),
    ],
)
def test_broken_days_are_rejected(mutate, message):
    records = price_records(PLAIN)
    mutate(records)
    with pytest.raises(ValidationError, match=message):
        parse_price_day(records, "SE3", PLAIN)


def test_day_must_start_at_local_midnight():
    with pytest.raises(ValidationError, match="day starts"):
        parse_price_day(price_records(PLAIN)[1:], "SE3", PLAIN)


def test_build_quarantines_bad_days_and_keeps_good_ones(data_dirs):
    good, bad = date(2025, 6, 1), date(2025, 6, 2)
    write_json(data_dirs / "bronze/prices/SE3/2025/2025-06-01.json", price_records(good))
    broken = price_records(bad)
    broken.pop(10)
    write_json(data_dirs / "bronze/prices/SE3/2025/2025-06-02.json", broken)

    stats = silver.build_prices()

    assert stats == {"months_built": 1, "days_ok": 1, "days_quarantined": 1}
    frame = pd.read_parquet(data_dirs / "silver/prices/zone=SE3/2025-06.parquet")
    assert set(frame["delivery_date"]) == {good}
    note = (data_dirs / "quarantine/prices/SE3_2025-06-02.json").read_text()
    assert "gap" in note


def test_quarantine_clears_once_the_day_is_fixed(data_dirs):
    day = date(2025, 6, 2)
    path = data_dirs / "bronze/prices/SE3/2025/2025-06-02.json"
    broken = price_records(day)
    broken.pop(10)
    write_json(path, broken)
    silver.build_prices()
    assert (data_dirs / "quarantine/prices/SE3_2025-06-02.json").exists()

    write_json(path, price_records(day))
    (data_dirs / "silver/prices/zone=SE3/2025-06.parquet").unlink(missing_ok=True)
    silver.build_prices()
    assert not (data_dirs / "quarantine/prices/SE3_2025-06-02.json").exists()


def test_unchanged_months_are_not_rebuilt(data_dirs):
    write_json(
        data_dirs / "bronze/prices/SE3/2025/2025-06-01.json", price_records(date(2025, 6, 1))
    )
    assert silver.build_prices()["months_built"] == 1
    assert silver.build_prices()["months_built"] == 0


# Silver: weather


def test_weather_payload_flattens_to_long_rows():
    frame = silver.parse_weather(weather_payload(date(2025, 1, 1), date(2025, 1, 2)))
    assert len(frame) == 8 * 48
    assert set(frame["zone"]) == {"SE1", "SE2", "SE3", "SE4"}
    assert str(frame["valid_at"].dt.tz) == "UTC"


def test_weather_with_missing_points_is_rejected():
    payload = weather_payload(date(2025, 1, 1), date(2025, 1, 1))[:5]
    with pytest.raises(ValidationError, match="weather points"):
        silver.parse_weather(payload)


def test_weather_archive_and_forecast_build(data_dirs):
    write_json(
        data_dirs / "bronze/weather/archive/2025-01.json",
        weather_payload(date(2025, 1, 1), date(2025, 1, 31)),
    )
    write_json(
        data_dirs / "bronze/weather/forecast/2025-01-31.json",
        {
            "fetched_at": "2025-01-31T10:00:00+01:00",
            "points": weather_payload(date(2025, 1, 31), date(2025, 2, 2)),
        },
    )
    write_json(data_dirs / "bronze/weather/forecast/2025-02-01.json", {"points": []})

    assert silver.build_weather_archive() == 1
    assert silver.build_weather_forecasts() == 1
    snap = pd.read_parquet(data_dirs / "silver/weather_forecast/2025-01-31.parquet")
    assert snap["issue_date"].iloc[0] == date(2025, 1, 31)
    assert (data_dirs / "quarantine/weather_forecast/2025-02-01.json").exists()
