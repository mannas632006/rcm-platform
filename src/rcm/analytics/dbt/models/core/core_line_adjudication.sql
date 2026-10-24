-- Adjudicated service lines, keyed to match the submitted ones.
--
-- Same occurrence key as `core_line_submission`, built over the 835's own
-- ordering. Only lines belonging to the primary (non-reversal) posting are
-- here; a reversal restates the same lines with every sign flipped and
-- would otherwise double the grain.
select
    sl.service_line_id,
    sl.client_id,
    sl.claim_payment_id,
    a.submitter_claim_id,
    sl.line_number as remittance_line_number,
    sl.procedure_code,
    sl.charge_amount as adjudicated_charge,
    sl.paid_amount as line_paid,
    sl.allowed_amount as line_allowed,
    sl.units as adjudicated_units,
    sl.remark_codes as rarc_codes,
    a.payment_date,
    a.claim_status_code,
    row_number() over (
        partition by sl.claim_payment_id, sl.procedure_code order by sl.line_number
    ) as procedure_occurrence
from {{ ref('stg_remittance_lines') }} as sl
inner join {{ ref('core_claim_adjudication') }} as a
    on sl.claim_payment_id = a.claim_payment_id
