-- SRS section 5.5. DOC-02's Dataset 1, made concrete.
--
-- **Grain: one row per submitted service line per adjustment reason** (PD-6).
-- A line adjusted under CO-45 and PR-96 is two rows. A line with no
-- adjustment is one row with null label columns. A line on a claim that was
-- never adjudicated -- rejected at the front end, or still in flight -- is
-- also one row, with every label null.
--
-- That last case matters and is easy to get wrong. Starting from the
-- remittance and joining outwards would silently drop every claim the payer
-- rejected, and a training set built from it would learn from a population
-- that excludes the claims most worth predicting. The model starts from what
-- was *submitted* and joins the outcome on, so an unadjudicated line is
-- present with a null label rather than absent.
--
-- **The column order follows the SRS**: keys, then features, then trailing
-- features, then labels. Columns whose source module does not exist yet are
-- present and typed with an explicit cast, and each says which milestone
-- fills it. "Every column present and typed" is the acceptance condition, and
-- a column that silently disappears until M4 is one that nothing downstream
-- can be written against.
with submitted as (
    select * from {{ ref('core_line_submission') }}
),

claims as (
    select * from {{ ref('core_claim_submission') }}
),

trailing_rates as (
    select * from {{ ref('core_trailing_denial_rates') }}
),

adjudicated_claim as (
    select * from {{ ref('core_claim_adjudication') }}
),

adjudicated_line as (
    select * from {{ ref('core_line_adjudication') }}
),

adjustments as (
    select * from {{ ref('core_line_adjustment') }}
),

joined as (
    select
        sl.claim_line_id,
        sl.procedure_occurrence,
        c.claim_id,
        c.client_id,
        c.submitter_claim_id,
        c.root_claim_id,
        c.submission_attempt,
        c.payer_id,
        c.plan_type,
        c.payer_sequence,
        c.is_secondary,
        c.billing_npi,
        c.place_of_service,
        c.date_of_service,
        c.claim_total_charge,
        c.lines_on_claim,
        c.primary_dx,
        c.dx_2,
        c.dx_3,
        c.dx_4,
        c.auth_number_present,
        c.referral_present,
        c.submission_date,
        c.submission_date_is_archival,
        c.submission_dow,
        c.submission_month,
        c.days_dos_to_submission,
        c.days_to_filing_deadline,

        sl.procedure_code,
        sl.modifier_1,
        sl.modifier_2,
        sl.modifier_3,
        sl.modifier_4,
        sl.units,
        sl.line_charge,
        sl.dx_pointer,

        t.payer_cpt_denial_rate_90d,
        t.payer_cpt_window_size,
        t.payer_provider_denial_rate_90d,
        t.payer_provider_window_size,
        t.cpt_modifier_denial_rate_90d,
        t.cpt_modifier_window_size,

        al.service_line_id,
        al.line_paid,
        al.line_allowed,
        al.rarc_codes,
        al.payment_date,
        ac.claim_status_code,
        ac.net_collected,
        ac.was_reversed,

        adj.adjustment_id,
        adj.cas_group_code,
        adj.carc_code,
        adj.adjustment_amount,
        adj.adjustment_sequence

    from submitted as sl
    inner join claims as c
        on sl.claim_id = c.claim_id
    left join trailing_rates as t
        on sl.claim_line_id = t.claim_line_id
    left join adjudicated_claim as ac
        on c.submitter_claim_id = ac.submitter_claim_id
        and c.client_id = ac.client_id
    left join adjudicated_line as al
        on ac.claim_payment_id = al.claim_payment_id
        and sl.procedure_code = al.procedure_code
        and sl.procedure_occurrence = al.procedure_occurrence
    -- The grain. A line with no adjustment keeps one row with nulls, so the
    -- join is LEFT and never INNER: an INNER here would drop every cleanly
    -- paid line from the dataset.
    left join adjustments as adj
        on al.service_line_id = adj.service_line_id
)

select
    -- ---- keys ------------------------------------------------------------
    j.service_line_id,
    j.claim_line_id,
    j.claim_id,
    j.client_id,
    j.adjustment_id,
    j.submitter_claim_id,
    j.root_claim_id,

    -- ---- features: all knowable at submission time (DR-07) ---------------
    j.payer_id,
    j.plan_type,
    j.payer_sequence,
    j.is_secondary,
    j.billing_npi,
    cast(null as varchar) as rendering_npi,   -- M1 gap: 837 loop 2310B unparsed
    cast(null as varchar) as taxonomy,        -- M1 gap: 837 loop 2000A PRV
    cast(null as varchar) as specialty,       -- M3: provider registry
    j.place_of_service,

    cast(null as varchar) as patient_age_bucket,  -- M3: patient module
    cast(null as varchar) as patient_sex,         -- M3: patient module
    cast(null as varchar) as patient_state,       -- M3: patient module

    j.procedure_code as cpt_hcpcs,
    j.modifier_1,
    j.modifier_2,
    j.modifier_3,
    j.modifier_4,
    j.units,
    j.primary_dx,
    j.dx_2,
    j.dx_3,
    j.dx_4,
    j.dx_pointer,
    cast(null as varchar) as ndc_code,        -- M1 gap: 837 LIN segment unparsed

    j.line_charge,
    j.claim_total_charge,
    j.lines_on_claim,
    j.days_dos_to_submission,
    j.days_to_filing_deadline,

    cast(null as boolean) as eligibility_checked,   -- M3: eligibility module
    cast(null as varchar) as eligibility_result,    -- M3: eligibility module
    cast(null as boolean) as auth_required,         -- M3: authorisation module
    j.auth_number_present,
    j.referral_present,
    cast(null as integer) as scrub_flags_raised,     -- M4: rules engine
    cast(null as integer) as scrub_flags_overridden, -- M4: rules engine

    j.submission_attempt,
    cast(null as varchar) as coder_id_hashed,  -- M3: the 837 names no coder
    j.submission_date,
    j.submission_dow,
    j.submission_month,

    -- ---- trailing features, computed AS OF submission date only ----------
    -- The window sizes travel with the rates on purpose. A rate of 1.00 over
    -- one observation and a rate of 1.00 over four hundred are different
    -- facts, and a model given only the rate cannot tell them apart.
    j.payer_cpt_denial_rate_90d,
    j.payer_cpt_window_size,
    j.payer_provider_denial_rate_90d,
    j.payer_provider_window_size,
    j.cpt_modifier_denial_rate_90d,
    j.cpt_modifier_window_size,

    -- ---- labels: from the 835, knowable only after adjudication ----------
    j.line_paid,
    j.line_allowed,
    j.cas_group_code,
    j.carc_code,
    j.rarc_codes,
    j.adjustment_amount,
    j.adjustment_sequence,
    j.claim_status_code,

    -- The denial signal, structural rather than taxonomic: the payer paid
    -- nothing on the line and gave a reason. Both halves matter. A line paid
    -- at zero with no reason code is not an adjudicated denial, and a reason
    -- code on a line that was paid is an adjustment, not a refusal.
    --
    -- Null, not false, when the line was never adjudicated. False would claim
    -- the payer accepted a line it never saw, and a training set reading it
    -- that way learns that rejected claims are clean ones (PD-4).
    case
        when j.service_line_id is null then null
        else (j.line_paid = 0 and j.adjustment_id is not null)
    end as is_denied,

    cast(null as varchar) as denial_category,  -- M4.8: denial taxonomy
    cast(null as varchar) as preventability,   -- M4.8: denial taxonomy

    case
        when j.payment_date is null or j.date_of_service is null then null
        else j.payment_date - j.date_of_service
    end as days_dos_to_payment,
    case
        when j.payment_date is null then null
        else j.payment_date - j.submission_date
    end as days_submission_to_adjudication,

    case
        when j.service_line_id is null then 'NOT_ADJUDICATED'
        when j.was_reversed then 'REVERSED'
        when j.line_paid > 0 then 'PAID'
        when j.adjustment_id is not null then 'DENIED'
        else 'ZERO_PAID_NO_REASON'
    end as final_outcome,

    -- BR-DATA-03: resubmissions chain, so the rework count is how many times
    -- this claim had already been sent. Attempt 1 is zero touches.
    j.submission_attempt - 1 as rework_touches,
    j.net_collected,
    cast(null as boolean) as underpayment_flag,  -- M4.10: contracted rates

    -- DR-09 / M2.6. Reversals and recoupments change outcomes retroactively,
    -- so a row is not usable for final-outcome training until this date has
    -- passed. The window is [TBD-50], a labelled placeholder, not a measured
    -- value.
    case
        when j.date_of_service is null then null
        else j.date_of_service + {{ var('outcome_maturity_days') }}
    end as outcome_matured_at,

    -- Provenance for the as-of date, so a corpus loaded by replay can be
    -- told from one loaded as it arrived. If this is true anywhere, the
    -- trailing features above were computed against an archival date and
    -- DR-07 is only approximately satisfied.
    j.submission_date_is_archival

from joined as j
