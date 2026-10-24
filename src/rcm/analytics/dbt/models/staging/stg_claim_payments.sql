-- CLP loop: what the payer decided.
--
-- **Post-submission.** Nothing selected here may become a feature, however
-- time-invariant it looks. `rendering_provider_npi` is the trap: the NPI was
-- a fact about the claim before it was sent, but this row did not exist
-- then, so reading it here would restrict the training set to adjudicated
-- claims and launder a payer-transformed value into a feature. The mart
-- leaves `rendering_npi` null until the 837 2310B loop is parsed.
select
    cp.claim_payment_id,
    cp.client_id,
    cp.remittance_id,
    cp.claim_id as submitter_claim_id,
    cp.payer_control_number,
    cp.status_code,
    cp.filing_indicator,
    cp.total_charge,
    cp.total_paid,
    cp.patient_responsibility,
    cp.service_date,
    cp.is_reversal,
    cp.reverses_claim_payment_id,
    r.payment_date,
    r.payer_id
from {{ source('core', 'claim_payment') }} as cp
inner join {{ source('core', 'remittance') }} as r
    on cp.remittance_id = r.remittance_id
{% if var('as_of_date', none) %}
-- M2.4: a remittance that arrived after the cutoff had not arrived yet, so
-- nothing may read it -- which is the whole point. Trailing windows are built
-- from adjudications by payment date, so this is the filter that decides
-- whether a feature can see the future.
where r.payment_date <= date '{{ var("as_of_date") }}'
{% endif %}
