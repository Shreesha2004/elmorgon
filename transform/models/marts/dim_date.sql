-- Calendar for delivery days, with Swedish public holidays.
with days as (
    select cast(d as date) as date_day
    from generate_series(date '2022-11-01', current_date + 7, interval 1 day) as t(d)
)

select
    days.date_day,
    year(days.date_day)                               as year,
    month(days.date_day)                              as month,
    isodow(days.date_day)                             as iso_dow,
    isodow(days.date_day) >= 6                        as is_weekend,
    h.holiday_date is not null                        as is_holiday,
    h.holiday_name,
    isodow(days.date_day) < 6 and h.holiday_date is null as is_workday
from days
left join {{ ref('swedish_holidays') }} as h
    on h.holiday_date = days.date_day
