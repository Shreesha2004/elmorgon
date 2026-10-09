select
    zone,
    zone_name,
    largest_city,
    description
from {{ ref('zones') }}
