-- DR-09. The maturity gate, asserted rather than assumed.
--
-- `mart_final_outcome` filters on `outcome_matured_at <= current_date`. This
-- says the filter did its job, which is worth stating separately because the
-- filter is one `where` clause and a future refactor that moves it into a
-- join could lose it without changing a single column name.
select
    claim_line_id,
    outcome_matured_at
from {{ ref('mart_final_outcome') }}
where outcome_matured_at > current_date
