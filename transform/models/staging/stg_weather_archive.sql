-- Archived short-range forecasts, one row per point and hour.
select
    point,
    zone,
    cast(valid_at as timestamptz) as valid_at,
    temperature_2m,
    wind_speed_100m,
    shortwave_radiation,
    cloud_cover
from {{ source('silver', 'weather_archive') }}
