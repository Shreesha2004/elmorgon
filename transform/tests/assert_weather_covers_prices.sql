-- At least 99% of priced hours must have weather. Returns a row when coverage drops.
with joined as (
    select count(*) as hours, count(w.zone) as with_weather
    from {{ ref('fct_price_hourly') }} as p
    left join {{ ref('fct_weather_hourly') }} as w
        on w.zone = p.zone and w.valid_at = p.hour_start
)

select hours, with_weather, with_weather / hours as coverage
from joined
where with_weather < 0.99 * hours
