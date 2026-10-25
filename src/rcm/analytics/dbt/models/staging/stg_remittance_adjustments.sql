-- CAS unrolled: one row per service line per adjustment reason (PD-6).
--
-- This is the grain of the whole mart. Collapsing it loses the thing the
-- platform exists to analyse -- a line adjusted under CO-45 and PR-96 is two
-- facts, and a single row carrying "45,96" cannot be grouped, filtered or
-- counted by either.
--
-- PD-3: `cas_group_code` is NOT NULL upstream and is never defaulted here.
-- CO-45 is a contractual write-off the patient may never be billed for;
-- PR-45 is an amount they owe. The group is not a qualifier on the reason,
-- it is half the meaning.
select
    a.adjustment_id,
    a.client_id,
    a.claim_payment_id,
    a.service_line_id,
    a.cas_group_code,
    a.reason_code,
    a.amount,
    a.quantity,
    a.claim_level,
    a.sequence
from {{ source('core', 'remittance_adjustment') }} as a
