-- One row per submission, as the 837 stated it.
--
-- Pre-submission source. Everything here was known the second before the
-- claim was sent, which is what makes it eligible to become a feature
-- (DR-07). `submission_date` is the date the submitter stated in BHT04, and
-- it is the as-of date every trailing window is computed against.
select
    c.claim_id,
    c.client_id,
    c.raw_file_id,
    c.submitter_claim_id,
    c.original_claim_id,
    c.root_claim_id,
    c.submission_attempt,
    c.claim_frequency_code,
    c.payer_id,
    c.payer_responsibility,
    c.claim_filing_code,
    c.billing_npi,
    c.billing_tax_id,
    c.subscriber_member_id,
    c.practice_identifier,
    nullif(c.place_of_service, '') as place_of_service,
    c.date_of_service,
    c.total_charge,
    -- PD-13: the numbers are kept, the identifiers themselves are not what a
    -- model reads. `*_present` is the feature; the value stays behind.
    nullif(c.prior_auth_number, '') is not null as auth_number_present,
    nullif(c.referral_number, '') is not null as referral_present,
    c.diagnoses,
    -- DR-07's as-of date. BHT04 is what the submitter stated; `received_at`
    -- is when this platform archived the file, and on a replay every row
    -- shares the latter. Preferring the business date is what keeps a
    -- backfilled corpus from producing empty trailing windows.
    coalesce(c.submitted_on, (f.received_at at time zone 'UTC')::date) as submission_date,
    c.submitted_on is null as submission_date_is_archival,
    f.received_at as archived_at
from {{ source('core', 'claim') }} as c
inner join {{ source('core', 'raw_file') }} as f
    on c.raw_file_id = f.raw_file_id
{% if var('as_of_date', none) %}
-- M2.4: the world as it looked on the cutoff. A claim sent after it had not
-- been sent yet.
where coalesce(c.submitted_on, (f.received_at at time zone 'UTC')::date)
      <= date '{{ var("as_of_date") }}'
{% endif %}
