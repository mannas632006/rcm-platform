-- Payer identity and the contract terms in force (PD-8: keyed on payer_id).
--
-- `timely_filing_days` is a term of the contract, so it was knowable at
-- submission and may feed a feature. M4.10 adds staleness: a registry entry
-- past its `last_verified_date` must not drive a BLOCKING decision, and the
-- date is carried here so that rule has something to read.
select
    p.payer_id,
    p.name as payer_name,
    nullif(p.plan_type, '') as plan_type,
    p.timely_filing_days,
    p.last_verified_date
from {{ source('core', 'payer') }} as p
