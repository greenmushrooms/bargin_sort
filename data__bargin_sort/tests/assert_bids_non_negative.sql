-- Bid figures come from free-text JSON via safe_numeric. NULL is fine and means
-- "not a number"; a negative would mean the cast let something through wrong.
select item_id, high_bid, min_bid, buy_now
from {{ ref('stg_lots') }}
where high_bid < 0 or min_bid < 0 or buy_now < 0
