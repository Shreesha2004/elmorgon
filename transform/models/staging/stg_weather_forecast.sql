-- Live forecasts as they were fetched on each issue day. Never revised.
select
    cast(issue_date as date)        as issue_date,
    cast(fetched_at as timestamptz) as fetched_at,
    point,
    zone,
    cast(valid_at as timestamptz)   as valid_at,
    temperature_2m,
    wind_speed_100m,
    shortwave_radiation,
    cloud_cover
from {{ source('silver', 'weather_forecast') }}
