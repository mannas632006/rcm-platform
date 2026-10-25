-- 999 and 277CA verdicts (M1.6). Post-submission.
--
-- PD-4: these are rejections, never denials. They are staged so rejection
-- reporting has a source, and they are deliberately absent from the mart's
-- feature columns -- a rejection is something the payer said after the claim
-- was sent.
select
    a.acknowledgement_id,
    a.client_id,
    a.raw_file_id,
    a.claim_id as submitter_claim_id,
    a.payer_claim_number,
    a.source as acknowledgement_source,
    a.outcome,
    nullif(a.category_code, '') as category_code,
    nullif(a.status_code, '') as status_code,
    a.effective_date,
    a.received_at
from {{ source('core', 'claim_acknowledgement') }} as a
