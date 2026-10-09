{{
    config(
        materialized='incremental',
        unique_key=['zone', 'hour_start'],
        incremental_strategy='delete+insert'
    )
}}

-- Grain: one row per zone and UTC hour. Sweden's UTC offset is a whole number
-- of hours, so a UTC hour is also exactly one local clock hour.
select
    zone,
    delivery_date,
    date_trunc('hour', interval_start)                                  as hour_start,
    hour(timezone('Europe/Stockholm', date_trunc('hour', interval_start))) as hour_local,
    avg(price_sek_kwh)                                                  as price_sek_kwh,
    min(price_sek_kwh)                                                  as min_interval_price,
    max(price_sek_kwh)                                                  as max_interval_price,
    count(*)                                                            as n_intervals
from {{ ref('fct_price_interval') }}
{% if is_incremental() %}
-- Recent days can be republished; rebuild the last three days on every run.
where delivery_date >= (select max(delivery_date) - 3 from {{ this }})
{% endif %}
group by all
