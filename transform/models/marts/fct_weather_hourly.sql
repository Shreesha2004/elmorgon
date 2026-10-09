-- Grain: one row per zone and hour; the average of that zone's weather points.
select
    zone,
    valid_at,
    avg(temperature_2m)      as temperature_c,
    avg(wind_speed_100m)     as wind_100m_ms,
    avg(shortwave_radiation) as solar_w_m2,
    avg(cloud_cover)         as cloud_cover_pct,
    count(*)                 as n_points
from {{ ref('stg_weather_archive') }}
group by zone, valid_at
