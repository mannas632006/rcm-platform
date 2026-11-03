-- The CARC -> denial category table (DOC-02 8.1, BR-TAX-01, ADR-0012),
-- taxonomy-v1.
--
-- The source is rcm.decision.denials.CARC_CATEGORY. These rows must equal it,
-- and tests/unit/test_denial_taxonomy.py::test_the_warehouse_reads_the_same_table
-- fails if they drift: one mapping, read in two places.
--
-- PAYER_ERROR has no row: no CARC says the payer was wrong. A person assigns
-- it when an appeal shows it.
select
    carc_code,
    denial_category,
    preventability
from (
    values
        ('4', 'CODING', 'PREVENTABLE'),
        ('11', 'CODING', 'PREVENTABLE'),
        ('15', 'AUTHORISATION', 'PREVENTABLE'),
        ('16', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('18', 'DUPLICATE', 'PREVENTABLE'),
        ('22', 'COORDINATION_OF_BENEFITS', 'PREVENTABLE'),
        ('26', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('27', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('29', 'TIMELY_FILING', 'PREVENTABLE'),
        ('31', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('49', 'CONTRACTUAL_NON_COVERED', 'NOT_PREVENTABLE'),
        ('50', 'MEDICAL_NECESSITY', 'PARTIALLY_PREVENTABLE'),
        ('54', 'CODING', 'PREVENTABLE'),
        ('55', 'MEDICAL_NECESSITY', 'PARTIALLY_PREVENTABLE'),
        ('58', 'CODING', 'PREVENTABLE'),
        ('59', 'CODING', 'PREVENTABLE'),
        ('96', 'CONTRACTUAL_NON_COVERED', 'NOT_PREVENTABLE'),
        ('97', 'CODING', 'PREVENTABLE'),
        ('109', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('119', 'CONTRACTUAL_NON_COVERED', 'NOT_PREVENTABLE'),
        ('140', 'REGISTRATION_ELIGIBILITY', 'PREVENTABLE'),
        ('146', 'CODING', 'PREVENTABLE'),
        ('147', 'PROVIDER_ENROLMENT', 'PREVENTABLE'),
        ('151', 'MEDICAL_NECESSITY', 'PARTIALLY_PREVENTABLE'),
        ('167', 'MEDICAL_NECESSITY', 'PARTIALLY_PREVENTABLE'),
        ('170', 'PROVIDER_ENROLMENT', 'PREVENTABLE'),
        ('181', 'CODING', 'PREVENTABLE'),
        ('182', 'CODING', 'PREVENTABLE'),
        ('183', 'PROVIDER_ENROLMENT', 'PREVENTABLE'),
        ('197', 'AUTHORISATION', 'PREVENTABLE'),
        ('198', 'AUTHORISATION', 'PREVENTABLE'),
        ('204', 'CONTRACTUAL_NON_COVERED', 'NOT_PREVENTABLE'),
        ('252', 'MEDICAL_NECESSITY', 'PARTIALLY_PREVENTABLE')
) as taxonomy (carc_code, denial_category, preventability)
