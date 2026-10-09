-- One row per zone and market interval (60 min until Sep 2025, 15 min after).
select
    cast(zone as varchar)               as zone,
    cast(delivery_date as date)         as delivery_date,
    cast(interval_start as timestamptz) as interval_start,
    cast(interval_end as timestamptz)   as interval_end,
    cast(resolution_minutes as integer) as resolution_minutes,
    price_sek_kwh,
    price_eur_kwh,
    eur_sek_rate
from {{ source('silver', 'prices') }}
