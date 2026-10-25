-- One row per service line as submitted.
--
-- DR-05: modifiers are positional, so they stay in four numbered columns
-- rather than an array. `25:59` and `59:25` are different claims and an
-- unordered collection cannot tell them apart.
select
    l.claim_line_id,
    l.client_id,
    l.claim_id,
    l.line_number,
    l.procedure_code,
    l.modifier_1,
    l.modifier_2,
    l.modifier_3,
    l.modifier_4,
    l.units,
    l.charge_amount,
    l.diagnosis_pointers,
    l.service_date,
    count(*) over (partition by l.claim_id) as lines_on_claim
from {{ source('core', 'claim_line') }} as l
{% if var('as_of_date', none) %}
-- M2.4: a line belongs to a claim, so a line whose claim had not been
-- submitted yet had not been submitted either. The cutoff is expressed once,
-- in `stg_claims`, and read from there rather than restated -- two copies of
-- the same date arithmetic is how the two builds end up describing slightly
-- different worlds, which would show up as a leak that is really a bug in
-- the test harness.
where l.claim_id in (select claim_id from {{ ref('stg_claims') }})
{% endif %}
