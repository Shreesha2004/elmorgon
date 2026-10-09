-- Every zone-day in the warehouse has exactly the intervals its local day length
-- allows (23, 24 or 25 hours at its resolution). Returns the days that do not.
with days as (
    select
        zone,
        delivery_date,
        any_value(resolution_minutes) as resolution_minutes,
        count(*)                      as intervals
    from {{ ref('fct_price_interval') }}
    group by zone, delivery_date
),

lengths as (
    select
        *,
        date_diff(
            'minute',
            timezone('Europe/Stockholm', cast(delivery_date as timestamp)),
            timezone('Europe/Stockholm', cast(delivery_date + 1 as timestamp))
        ) as day_minutes
    from days
)

select *
from lengths
where intervals * resolution_minutes != day_minutes
