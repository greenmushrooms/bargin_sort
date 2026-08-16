-- The view is the product boundary: nothing beyond 50 km may appear in it,
-- whatever HiBid returned. Sponsored listings ignore the radius parameter.
select item_id, distance_km
from {{ ref('lots_within_50km') }}
where distance_km > 50 or distance_km < 0
