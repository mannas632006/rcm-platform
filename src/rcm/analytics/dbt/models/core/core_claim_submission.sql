-- One row per submission, with every pre-submission fact derived.
--
-- Everything in this model was knowable the second before the claim was sent
-- (DR-07). The only joins are to `claim`, `payer` and the 837's own
-- `raw_file`; no remittance table is reachable from here, which is how the
-- leakage suite can make a structural argument rather than a name-based one.
with claims as (
    select * from {{ ref('stg_claims') }}
),

payers as (
    select * from {{ ref('stg_payers') }}
),

line_rollup as (
    select
        claim_id,
        count(*) as lines_on_claim
    from {{ ref('stg_claim_lines') }}
    group by claim_id
)

select
    c.claim_id,
    c.client_id,
    c.submitter_claim_id,
    c.root_claim_id,
    c.original_claim_id,
    c.submission_attempt,
    c.claim_frequency_code,

    -- payer identity and the contract terms in force (PD-8)
    c.payer_id,
    p.plan_type,
    c.payer_responsibility as payer_sequence,
    c.payer_responsibility <> 'P' as is_secondary,
    c.claim_filing_code,

    -- provider. `rendering_npi` is deliberately absent: the 837 2310B loop is
    -- not parsed yet, and the only other source for it is the 835, which did
    -- not exist at submission.
    c.billing_npi,
    c.practice_identifier,
    c.place_of_service,

    -- the claim as submitted
    c.date_of_service,
    c.total_charge as claim_total_charge,
    coalesce(r.lines_on_claim, 0) as lines_on_claim,
    c.diagnoses,
    c.diagnoses[1] as primary_dx,
    c.diagnoses[2] as dx_2,
    c.diagnoses[3] as dx_3,
    c.diagnoses[4] as dx_4,
    c.auth_number_present,
    c.referral_present,

    -- timing, all measured from the business submission date
    c.submission_date,
    c.submission_date_is_archival,
    c.archived_at,
    extract(isodow from c.submission_date)::smallint as submission_dow,
    extract(month from c.submission_date)::smallint as submission_month,
    case
        when c.date_of_service is null then null
        else (c.submission_date - c.date_of_service)
    end as days_dos_to_submission,

    -- BR/PD-15's input. Negative means the claim was already late when it was
    -- sent, which is a real and important state rather than a data error, so
    -- it is not clamped at zero.
    case
        when c.date_of_service is null or p.timely_filing_days is null then null
        else p.timely_filing_days - (c.submission_date - c.date_of_service)
    end as days_to_filing_deadline

from claims as c
left join payers as p on c.payer_id = p.payer_id
left join line_rollup as r on c.claim_id = r.claim_id
