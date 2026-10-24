-- Every other test in this project passes on an empty table.
--
-- That is how dbt data tests work: they select the rows that violate an
-- invariant and fail if any come back. With no rows at all, nothing
-- violates anything, and a build against an empty warehouse reports a clean
-- sweep. The M2 mutation pass found exactly this -- four controls came back
-- "not proven" because the harness had rebuilt against a database another
-- suite had truncated, and every assertion went green over nothing.
--
-- So this test is the floor the others stand on. It is the same argument as
-- the synthetic corpus being required to *contain* rejections rather than
-- merely permitted to (M1.6): a suite that cannot tell "nothing is wrong"
-- from "nothing is there" is not a suite.
select
    'mart_claim_line_outcome is empty' as problem
where not exists (select 1 from {{ ref('mart_claim_line_outcome') }})
