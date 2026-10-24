-- DR-07: trailing windows computed AS OF the submission timestamp. Nothing else.
--
--     "Could this value have been known the second before we pressed SEND?"
--
-- The trap this model exists to avoid is subtle enough to be worth spelling
-- out, because the obvious implementation is wrong and looks right.
--
-- A denial rate "over the last 90 days" is naturally written as a group-by
-- over claims *submitted* in that window. That is leakage. A claim submitted
-- 10 days ago may not have been adjudicated yet; its outcome was not known at
-- the moment this claim was sent, and including it means the feature carries
-- information from the future. The model scores beautifully in test and is
-- worthless in production, which is the failure DR-07 describes.
--
-- So the window is over **adjudications, by payment date**, not submissions
-- by submission date. An adjudicated line enters the history on the day the
-- payer's decision arrived, because that is the day anyone could have known
-- it.
--
-- The window is `[as_of - 90 days, as_of - 1 day]`. The current day is
-- excluded rather than included: a remittance that landed earlier the same
-- morning was knowable, but at date granularity "same day" cannot be told
-- from "a few hours later", and PD-18 says to fail closed. The cost is a
-- slightly staler feature; the alternative is a leak that nothing detects.
--
-- The shape is a single ordered stream rather than a correlated subquery
-- per row. A correlated version is O(n^2) and does not finish on a real
-- corpus; this is one pass per partition.
with adjudicated as (
    select
        sub.payer_id,
        sub.billing_npi,
        ls.procedure_code,
        ls.modifier_1,
        la.payment_date as known_on,
        -- The denial rule, structural and the same one M1.9 established: the
        -- payer paid nothing on the line and gave a reason. A zero payment
        -- with no reason code is not an adjudicated denial, and a reason code
        -- on a line that was paid is an adjustment, not a refusal.
        case
            when la.line_paid = 0 and adj.adjustment_id is not null then 1
            else 0
        end as denied
    from {{ ref('core_line_adjudication') }} as la
    inner join {{ ref('core_claim_adjudication') }} as ca
        on la.claim_payment_id = ca.claim_payment_id
    inner join {{ ref('core_claim_submission') }} as sub
        on ca.submitter_claim_id = sub.submitter_claim_id
        and ca.client_id = sub.client_id
    inner join {{ ref('core_line_submission') }} as ls
        on ls.claim_id = sub.claim_id
        and ls.procedure_code = la.procedure_code
        and ls.procedure_occurrence = la.procedure_occurrence
    left join lateral (
        select a.adjustment_id
        from {{ ref('core_line_adjustment') }} as a
        where a.service_line_id = la.service_line_id
        limit 1
    ) as adj on true
    where la.payment_date is not null
),

queries as (
    -- One row per submitted line: the question "what was the rate on the day
    -- this went out?"
    select
        ls.claim_line_id,
        sub.payer_id,
        sub.billing_npi,
        ls.procedure_code,
        ls.modifier_1,
        sub.submission_date as as_of
    from {{ ref('core_line_submission') }} as ls
    inner join {{ ref('core_claim_submission') }} as sub
        on ls.claim_id = sub.claim_id
),

stream as (
    select
        claim_line_id,
        payer_id,
        billing_npi,
        procedure_code,
        modifier_1,
        as_of as event_date,
        true as is_query,
        0 as denied,
        0 as adjudicated
    from queries

    union all

    select
        cast(null as uuid) as claim_line_id,
        payer_id,
        billing_npi,
        procedure_code,
        modifier_1,
        known_on as event_date,
        false as is_query,
        denied,
        1 as adjudicated
    from adjudicated
),

windowed as (
    select
        claim_line_id,
        is_query,
        -- `sum()` over an empty frame returns NULL, not 0, and the two mean
        -- different things here. Zero says "we looked back ninety days and
        -- there was nothing" -- a real observation, and the state every
        -- payer/code pair starts in. NULL says "not known". A model handed
        -- NULL for both cannot tell a brand-new code from one it failed to
        -- measure, so the counts are coalesced and only the *rate* stays
        -- null when there is nothing to divide by.
        coalesce(sum(denied) over w_payer_cpt, 0) as denied_payer_cpt,
        coalesce(sum(adjudicated) over w_payer_cpt, 0) as seen_payer_cpt,
        coalesce(sum(denied) over w_payer_provider, 0) as denied_payer_provider,
        coalesce(sum(adjudicated) over w_payer_provider, 0) as seen_payer_provider,
        coalesce(sum(denied) over w_cpt_modifier, 0) as denied_cpt_modifier,
        coalesce(sum(adjudicated) over w_cpt_modifier, 0) as seen_cpt_modifier
    from stream
    window
        w_payer_cpt as (
            partition by payer_id, procedure_code
            order by event_date
            range between interval '90 days' preceding and interval '1 day' preceding
        ),
        w_payer_provider as (
            partition by payer_id, billing_npi
            order by event_date
            range between interval '90 days' preceding and interval '1 day' preceding
        ),
        w_cpt_modifier as (
            partition by procedure_code, modifier_1
            order by event_date
            range between interval '90 days' preceding and interval '1 day' preceding
        )
)

select
    claim_line_id,
    -- Null, not zero, when the window held no adjudications. Zero would say
    -- "this payer never denies this code", which is a strong and false claim
    -- to make from no evidence; null says "not known", and a model that needs
    -- a value can impute one deliberately (PD-18).
    case
        when seen_payer_cpt > 0
            then round(denied_payer_cpt::numeric / seen_payer_cpt, 6)
    end as payer_cpt_denial_rate_90d,
    seen_payer_cpt as payer_cpt_window_size,
    case
        when seen_payer_provider > 0
            then round(denied_payer_provider::numeric / seen_payer_provider, 6)
    end as payer_provider_denial_rate_90d,
    seen_payer_provider as payer_provider_window_size,
    case
        when seen_cpt_modifier > 0
            then round(denied_cpt_modifier::numeric / seen_cpt_modifier, 6)
    end as cpt_modifier_denial_rate_90d,
    seen_cpt_modifier as cpt_modifier_window_size
from windowed
where is_query
