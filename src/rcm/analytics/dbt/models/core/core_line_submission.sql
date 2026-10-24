-- Submitted service lines, with the key that survives a payer reordering.
--
-- `remittance_service_line.line_number` is the ordinal position in the 835,
-- not the line number the 837 sent -- the loader assigns it by enumerating
-- the SVC loops. Joining the two on that number is right only while the
-- payer returns every line, in order, which the generator does and real
-- payers do not: lines get bundled, split and dropped.
--
-- So the join key is (claim, procedure code, nth occurrence of that code).
-- It is robust to the payer dropping or reordering a *different* procedure,
-- and it handles the same CPT appearing twice on one claim, which is common
-- and which a procedure-code-only join would collapse.
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
    l.charge_amount as line_charge,
    l.diagnosis_pointers,
    l.diagnosis_pointers[1] as dx_pointer,
    l.service_date,
    row_number() over (
        partition by l.claim_id, l.procedure_code order by l.line_number
    ) as procedure_occurrence
from {{ ref('stg_claim_lines') }} as l
