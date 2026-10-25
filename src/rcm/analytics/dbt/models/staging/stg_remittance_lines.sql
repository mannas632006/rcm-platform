-- SVC loop. Post-submission.
select
    sl.service_line_id,
    sl.client_id,
    sl.claim_payment_id,
    sl.line_number,
    sl.procedure_code,
    sl.modifiers,
    sl.charge_amount,
    sl.paid_amount,
    sl.allowed_amount,
    sl.units,
    sl.service_date,
    sl.remark_codes
from {{ source('core', 'remittance_service_line') }} as sl
