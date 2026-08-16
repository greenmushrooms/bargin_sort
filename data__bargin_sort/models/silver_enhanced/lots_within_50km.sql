{{ config(materialized='view') }}

-- The search the whole pipeline exists to answer.
--
-- A view, not a table: the radius is the one thing likely to be tweaked, and a
-- view costs nothing to redefine. Lots whose postal code did not geocode are
-- excluded rather than assumed near — a null distance is unknown, not zero.

select *
from {{ ref('fct_lots') }}
where distance_km is not null
  and distance_km <= 50
