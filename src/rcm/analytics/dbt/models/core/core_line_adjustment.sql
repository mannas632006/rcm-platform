-- PD-6: one row per service line per adjustment reason. The grain.
--
-- Claim-level adjustments (CAS at the CLP loop rather than the SVC loop)
-- carry a null `service_line_id` upstream and are excluded here: they belong
-- to the claim, and attaching them to an arbitrary line would invent a
-- precision the 835 does not have. They remain available in staging for
-- claim-grain reporting.
select
    adj.adjustment_id,
    adj.client_id,
    adj.claim_payment_id,
    adj.service_line_id,
    adj.cas_group_code,
    adj.reason_code as carc_code,
    adj.amount as adjustment_amount,
    adj.quantity as adjustment_quantity,
    adj.sequence as adjustment_sequence
from {{ ref('stg_remittance_adjustments') }} as adj
where not adj.claim_level
