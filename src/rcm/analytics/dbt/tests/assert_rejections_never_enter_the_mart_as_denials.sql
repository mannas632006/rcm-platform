-- PD-4, carried into the warehouse.
--
-- A claim the payer rejected never reached adjudication, so it has no 835
-- and `is_denied` must be null rather than false or true. Null says "not
-- adjudicated"; false would claim the payer accepted a line it never saw,
-- and a training set reading it that way learns that rejected claims are
-- clean ones.
select
    claim_line_id,
    final_outcome,
    is_denied
from {{ ref('mart_claim_line_outcome') }}
where final_outcome = 'NOT_ADJUDICATED'
  and is_denied is not null
