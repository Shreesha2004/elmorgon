-- Grain: one row per zone and market interval, at the resolution the market cleared.
select
    zone,
    delivery_date,
    interval_start,
    interval_end,
    timezone('Europe/Stockholm', interval_start) as interval_start_local,
    resolution_minutes,
    price_sek_kwh,
    price_eur_kwh,
    eur_sek_rate
from {{ ref('stg_prices') }}
