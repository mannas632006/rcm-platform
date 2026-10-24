-- BR-M9-04: a reversal restates a claim in order to take the payment back.
--
-- It is not a second adjudication, and if reversal postings reached the mart
-- a reversed line would appear twice -- once paid, once negated -- with
-- distinct ids both times. Every uniqueness test in the project would still
-- pass, because the rows really are distinct; the grain would simply be
-- wrong, and a denial rate computed over it would count reversed claims
-- twice.
--
-- `core_claim_adjudication` filters to the primary posting. This asserts the
-- filter is doing its job from the other end.
select
    claim_line_id,
    count(distinct service_line_id) as adjudications
from {{ ref('mart_claim_line_outcome') }}
where service_line_id is not null
group by claim_line_id
having count(distinct service_line_id) > 1
