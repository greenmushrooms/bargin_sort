-- stg_hibid_lots' grain is one row per lot per run. A duplicate means pagination
-- yielded the same lot twice within a single scrape, which would double-count
-- inventory downstream.
select sys_run_name, item_id, count(*) as rows
from {{ ref('stg_hibid_lots') }}
group by 1, 2
having count(*) > 1
