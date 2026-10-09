-- Grain: one row per zone and target hour.
--
-- Point in time: the forecast for delivery day D+1 is issued at 10:00 on D.
-- Every feature below uses only prices up to the end of D (published the day
-- before), the calendar, and a weather forecast for the target hour. Prices
-- are joined at offsets of one day or more, never the same day.

with hourly as (
    select zone, delivery_date, hour_start, hour_local, price_sek_kwh
    from {{ ref('fct_price_hourly') }}
),

last_day as (
    select max(delivery_date) as d from hourly
),

-- The next delivery day, not yet published: the rows a live forecast fills.
-- 26 candidate hours cover the 25-hour DST day; the filter keeps the real ones.
next_day as (
    select
        z.zone,
        cast(last_day.d + 1 as date) as delivery_date,
        timezone('Europe/Stockholm', cast(last_day.d + 1 as timestamp))
            + to_hours(k) as hour_start
    from {{ ref('dim_zone') }} as z
    cross join last_day
    cross join (select unnest(range(0, 26)) as k)
),

targets as (
    select zone, delivery_date, hour_start, hour_local, price_sek_kwh
    from hourly
    union all
    select
        zone,
        delivery_date,
        hour_start,
        hour(timezone('Europe/Stockholm', hour_start)),
        cast(null as double)
    from next_day
    where cast(timezone('Europe/Stockholm', hour_start) as date) = delivery_date
),

-- The 25-hour DST day repeats one local hour; average it so lags join one-to-one.
by_local_hour as (
    select zone, delivery_date, hour_local, avg(price_sek_kwh) as price
    from hourly
    group by all
),

daily as (
    select
        zone,
        delivery_date,
        avg(price_sek_kwh)                                              as day_mean,
        min(price_sek_kwh)                                              as day_min,
        max(price_sek_kwh)                                              as day_max,
        stddev_samp(price_sek_kwh)                                      as day_std,
        avg(price_sek_kwh) filter (where hour_local between 17 and 20)  as day_peak_mean
    from hourly
    group by all
),

daily_windowed as (
    select
        *,
        avg(day_mean) over (
            partition by zone order by delivery_date
            range between interval 6 days preceding and current row
        ) as week_mean,
        day_mean - lag(day_mean) over (partition by zone order by delivery_date) as day_mean_change
    from daily
),

-- Yesterday's level in the neighbouring zones: prices move together across the Nordics.
cross_zone as (
    select
        delivery_date,
        avg(day_mean) filter (where zone = 'SE1') as se1_day_mean,
        avg(day_mean) filter (where zone = 'SE3') as se3_day_mean,
        avg(day_mean) filter (where zone = 'SE4') as se4_day_mean
    from daily
    group by delivery_date
),

-- Archived forecasts for history; the snapshot taken on the issue day for the live row.
live_snapshot as (
    select max(issue_date) as issue_date
    from {{ ref('stg_weather_forecast') }}
    where issue_date <= (select d from last_day)
),

weather as (
    select zone, valid_at, temperature_c, wind_100m_ms, solar_w_m2, cloud_cover_pct,
           'archive' as weather_source
    from {{ ref('fct_weather_hourly') }}
    where cast(timezone('Europe/Stockholm', valid_at) as date) <= (select d from last_day)
    union all
    select zone, valid_at, avg(temperature_2m), avg(wind_speed_100m), avg(shortwave_radiation),
           avg(cloud_cover), 'forecast'
    from {{ ref('stg_weather_forecast') }}
    where issue_date = (select issue_date from live_snapshot)
      and cast(timezone('Europe/Stockholm', valid_at) as date) > (select d from last_day)
    group by zone, valid_at
),

-- Northern wind drives the whole system's price, not just SE1 and SE2.
north_wind as (
    select valid_at, avg(wind_100m_ms) as north_wind_ms
    from weather
    where zone in ('SE1', 'SE2')
    group by valid_at
),

weather_day as (
    select
        zone,
        cast(timezone('Europe/Stockholm', valid_at) as date) as day,
        avg(temperature_c)                                    as day_temp_c,
        avg(wind_100m_ms)                                     as day_wind_ms
    from weather
    group by all
)

select
    t.zone,
    t.delivery_date,
    t.hour_start,
    t.hour_local,
    t.delivery_date - 1         as issue_date,
    t.price_sek_kwh             as target_price,

    d.iso_dow,
    d.month,
    d.is_weekend,
    d.is_holiday,
    d.is_workday,

    l1.price                    as lag_1d,
    l2.price                    as lag_2d,
    l7.price                    as lag_7d,

    dw.day_mean                 as prev_day_mean,
    dw.day_min                  as prev_day_min,
    dw.day_max                  as prev_day_max,
    dw.day_std                  as prev_day_std,
    dw.day_peak_mean            as prev_day_peak_mean,
    dw.week_mean                as prev_week_mean,
    dw.day_mean_change          as prev_day_mean_change,

    cz.se1_day_mean             as prev_se1_mean,
    cz.se3_day_mean             as prev_se3_mean,
    cz.se4_day_mean             as prev_se4_mean,

    w.temperature_c,
    w.wind_100m_ms,
    w.solar_w_m2,
    w.cloud_cover_pct,
    w.weather_source,
    nw.north_wind_ms,
    wd.day_temp_c               as target_day_temp_c,
    wd.day_wind_ms              as target_day_wind_ms

from targets as t
join {{ ref('dim_date') }} as d on d.date_day = t.delivery_date
left join by_local_hour as l1
    on l1.zone = t.zone and l1.delivery_date = t.delivery_date - 1 and l1.hour_local = t.hour_local
left join by_local_hour as l2
    on l2.zone = t.zone and l2.delivery_date = t.delivery_date - 2 and l2.hour_local = t.hour_local
left join by_local_hour as l7
    on l7.zone = t.zone and l7.delivery_date = t.delivery_date - 7 and l7.hour_local = t.hour_local
left join daily_windowed as dw
    on dw.zone = t.zone and dw.delivery_date = t.delivery_date - 1
left join cross_zone as cz
    on cz.delivery_date = t.delivery_date - 1
left join weather as w
    on w.zone = t.zone and w.valid_at = t.hour_start
left join north_wind as nw
    on nw.valid_at = t.hour_start
left join weather_day as wd
    on wd.zone = t.zone and wd.day = t.delivery_date
-- The first week has no weekly lag yet.
where t.delivery_date >= (select min(delivery_date) + 7 from hourly)
