-- PD-3, asserted in the warehouse as well as in the database.
--
-- The CORE constraint already refuses the row, so this should never fire --
-- which is the point. A mart is a rewrite of the data, and a join or a
-- coalesce can lose a column the source held correctly. This test says the
-- invariant survived the rewrite.
--
-- CO-45 is a contractual write-off the patient may never be billed for
-- (PD-19). PR-45 is an amount they owe. A CARC without its group is not
-- incomplete, it is unsafe.
select
    adjustment_id,
    carc_code,
    cas_group_code
from {{ ref('mart_claim_line_outcome') }}
where carc_code is not null
  and cas_group_code is null
