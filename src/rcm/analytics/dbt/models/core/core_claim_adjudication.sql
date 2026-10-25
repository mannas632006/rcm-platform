-- The payer's decision on a claim, with reversals kept separate.
--
-- BR-M9-04: a reversal negates an earlier posting; it is not a second
-- adjudication and it is not a new claim. The primary posting is the first
-- non-reversal one, and everything the reversal does shows up in
-- `net_collected` and in the claim being reopened -- not by replacing the
-- original decision, which would lose the fact that it was ever made.
with payments as (
    select * from {{ ref('stg_claim_payments') }}
),

primary_posting as (
    select
        *,
        row_number() over (
            partition by client_id, submitter_claim_id order by payment_date, claim_payment_id
        ) as posting_rank
    from payments
    where not is_reversal
),

money as (
    -- Reversals carry negative amounts, so summing everything gives what was
    -- actually collected after the money moved back.
    select
        client_id,
        submitter_claim_id,
        sum(total_paid) as net_collected,
        count(*) filter (where is_reversal) as reversal_count
    from payments
    group by client_id, submitter_claim_id
)

select
    p.claim_payment_id,
    p.client_id,
    p.remittance_id,
    p.submitter_claim_id,
    p.payer_control_number,
    p.status_code as claim_status_code,
    p.total_charge as adjudicated_charge,
    p.total_paid as claim_paid,
    p.patient_responsibility,
    p.payment_date,
    m.net_collected,
    m.reversal_count,
    m.reversal_count > 0 as was_reversed
from primary_posting as p
inner join money as m
    on p.client_id = m.client_id and p.submitter_claim_id = m.submitter_claim_id
where p.posting_rank = 1
