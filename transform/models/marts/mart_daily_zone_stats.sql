-- Daily price profile per zone: how much the price moves inside a day, and
-- how often it goes negative. Read by the dashboard.
select
    p.delivery_date,
    p.zone,
    any_value(p.resolution_minutes)                       as resolution_minutes,
    avg(p.price_sek_kwh)                                  as mean_price,
    min(p.price_sek_kwh)                                  as min_price,
    max(p.price_sek_kwh)                                  as max_price,
    max(p.price_sek_kwh) - min(p.price_sek_kwh)           as intraday_spread,
    count(*) filter (where p.price_sek_kwh < 0)           as negative_intervals,
    arg_min(p.interval_start_local, p.price_sek_kwh)      as cheapest_interval_local,
    arg_max(p.interval_start_local, p.price_sek_kwh)      as dearest_interval_local,
    d.is_workday
from {{ ref('fct_price_interval') }} as p
join {{ ref('dim_date') }} as d on d.date_day = p.delivery_date
group by p.delivery_date, p.zone, d.is_workday
