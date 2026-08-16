-- HiBid lots must always resolve to an auction: a null one means extraction
-- picked up a featured or related item from another auction, which can never
-- be placed in a radius. Scoped to hibid because a single-seller source
-- legitimately has no auction to point at.
select item_id, source
from {{ ref('fct_lots') }}
where source = 'hibid'
  and auction_id is null
