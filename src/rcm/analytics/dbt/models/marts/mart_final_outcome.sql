-- DR-09 / M2.6: the rows whose answer has stopped moving.
--
--     "No row enters final-outcome training until `outcome_matured_at` has
--      passed (recommended 180 days post-DOS), because reversals and
--      recoupments change outcomes retroactively."
--
-- A claim paid in March and recouped in September was, in April, a paid
-- claim. Anything that read it in April read it correctly and still got the
-- wrong answer, because the answer had not finished happening. This model is
-- the one that final-outcome metrics and training read from; the unfiltered
-- mart remains for operational reporting, where "what do we believe today"
-- is the right question.
--
-- The window is `{{ var('outcome_maturity_days') }}` days, which is
-- [TBD-50] -- DR-09's recommendation, not a measurement of this book of
-- business. It is a variable so that the sponsor's decision is a config
-- change.
--
-- Rows with no date of service cannot be aged and are excluded. Fail closed
-- (PD-18): admitting them would admit exactly the rows nothing can vouch for.
select
    *,
    -- Carried so a consumer can see which rule let the row in, rather than
    -- inferring it from the row's absence somewhere else.
    {{ var('outcome_maturity_days') }} as maturity_window_days
from {{ ref('mart_claim_line_outcome') }}
where outcome_matured_at is not null
  and outcome_matured_at <= current_date
