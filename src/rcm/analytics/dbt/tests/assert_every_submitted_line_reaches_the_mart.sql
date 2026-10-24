-- The dataset must not silently exclude anything that was submitted.
--
-- The mart starts from submitted lines and joins outcomes on, with every
-- join LEFT. Turning one of them INNER would be an easy and plausible
-- change -- it reads as tightening -- and it would drop two populations at
-- once: lines the payer never adjudicated (PD-4 rejections), and lines that
-- were paid cleanly with no adjustment at all.
--
-- Both omissions bias the dataset in the same direction, towards claims
-- that went wrong, and neither changes any column or any name. Nothing else
-- in the suite would notice, so this test counts.
select
    ls.claim_line_id
from {{ ref('core_line_submission') }} as ls
left join {{ ref('mart_claim_line_outcome') }} as m
    on ls.claim_line_id = m.claim_line_id
where m.claim_line_id is null
