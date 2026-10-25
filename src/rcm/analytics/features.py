"""What every mart column is, and which of them a model may read (M2.4).

    PD-11: Features must be knowable at submission time. No leakage.

    DR-07 (MUST): All trailing-window features MUST be computed with an
    as-of date equal to the claim's submission timestamp. Computing them
    over the full history is leakage.

The SRS calls this *"the requirement most likely to be violated accidentally"*,
and the accident is always the same shape. Someone adds a column that is
obviously useful, it is obviously about the claim, and it happens to be
sourced from a document that did not exist when the claim was sent. The model
reads the answer, scores beautifully in test, and is worthless in production.
Nothing in the pipeline objects, because nothing in the pipeline knows which
columns are supposed to be answers.

This registry is what knows. Every column in `mart_claim_line_outcome` is
classified here, and `tests/leakage` enforces two things against it:

1. **The mart and the registry agree exactly.** A new column that nobody has
   classified fails the suite. You cannot add a feature without saying it is
   one, which is the moment the question gets asked.

2. **No FEATURE changes when the future is removed.** The warehouse is built
   twice -- once whole, once as it looked on a cutoff date -- and every
   feature value for the rows present in both must be identical. This is
   DR-07's question asked empirically rather than argued: if recomputing a
   value without any data that arrived after submission gives a different
   answer, the value could not have been known at submission.

The second test is the one that catches what review misses, because it does
not care how a column was written or what it is called.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ColumnRole(StrEnum):
    """What a mart column is for."""

    #: Identifies the row. Not read by a model; used to join and to audit.
    KEY = "KEY"
    #: A model may read this. It was knowable the second before SEND.
    FEATURE = "FEATURE"
    #: The answer. Knowable only after the payer adjudicated.
    LABEL = "LABEL"
    #: Describes how the row was built, not the claim. Never a model input,
    #: and never an answer either.
    PROVENANCE = "PROVENANCE"


#: Every column of `mart_claim_line_outcome`, and what it is.
#:
#: Kept in SRS section 5.5's order so the two can be read side by side.
MART_COLUMNS: Final[dict[str, ColumnRole]] = {
    # ---- keys --------------------------------------------------------------
    "service_line_id": ColumnRole.KEY,
    "claim_line_id": ColumnRole.KEY,
    "claim_id": ColumnRole.KEY,
    "client_id": ColumnRole.KEY,
    "adjustment_id": ColumnRole.KEY,
    "submitter_claim_id": ColumnRole.KEY,
    "root_claim_id": ColumnRole.KEY,
    # ---- features ----------------------------------------------------------
    "payer_id": ColumnRole.FEATURE,
    "plan_type": ColumnRole.FEATURE,
    "payer_sequence": ColumnRole.FEATURE,
    "is_secondary": ColumnRole.FEATURE,
    "billing_npi": ColumnRole.FEATURE,
    "rendering_npi": ColumnRole.FEATURE,
    "taxonomy": ColumnRole.FEATURE,
    "specialty": ColumnRole.FEATURE,
    "place_of_service": ColumnRole.FEATURE,
    "patient_age_bucket": ColumnRole.FEATURE,
    "patient_sex": ColumnRole.FEATURE,
    "patient_state": ColumnRole.FEATURE,
    "cpt_hcpcs": ColumnRole.FEATURE,
    "modifier_1": ColumnRole.FEATURE,
    "modifier_2": ColumnRole.FEATURE,
    "modifier_3": ColumnRole.FEATURE,
    "modifier_4": ColumnRole.FEATURE,
    "units": ColumnRole.FEATURE,
    "primary_dx": ColumnRole.FEATURE,
    "dx_2": ColumnRole.FEATURE,
    "dx_3": ColumnRole.FEATURE,
    "dx_4": ColumnRole.FEATURE,
    "dx_pointer": ColumnRole.FEATURE,
    "ndc_code": ColumnRole.FEATURE,
    "line_charge": ColumnRole.FEATURE,
    "claim_total_charge": ColumnRole.FEATURE,
    "lines_on_claim": ColumnRole.FEATURE,
    "days_dos_to_submission": ColumnRole.FEATURE,
    "days_to_filing_deadline": ColumnRole.FEATURE,
    "eligibility_checked": ColumnRole.FEATURE,
    "eligibility_result": ColumnRole.FEATURE,
    "auth_required": ColumnRole.FEATURE,
    "auth_number_present": ColumnRole.FEATURE,
    "referral_present": ColumnRole.FEATURE,
    "scrub_flags_raised": ColumnRole.FEATURE,
    "scrub_flags_overridden": ColumnRole.FEATURE,
    "submission_attempt": ColumnRole.FEATURE,
    "coder_id_hashed": ColumnRole.FEATURE,
    "submission_date": ColumnRole.FEATURE,
    "submission_dow": ColumnRole.FEATURE,
    "submission_month": ColumnRole.FEATURE,
    # ---- trailing features, computed as-of submission date (DR-07) ---------
    "payer_cpt_denial_rate_90d": ColumnRole.FEATURE,
    "payer_cpt_window_size": ColumnRole.FEATURE,
    "payer_provider_denial_rate_90d": ColumnRole.FEATURE,
    "payer_provider_window_size": ColumnRole.FEATURE,
    "cpt_modifier_denial_rate_90d": ColumnRole.FEATURE,
    "cpt_modifier_window_size": ColumnRole.FEATURE,
    # ---- labels ------------------------------------------------------------
    "line_paid": ColumnRole.LABEL,
    "line_allowed": ColumnRole.LABEL,
    "cas_group_code": ColumnRole.LABEL,
    "carc_code": ColumnRole.LABEL,
    "rarc_codes": ColumnRole.LABEL,
    "adjustment_amount": ColumnRole.LABEL,
    "adjustment_sequence": ColumnRole.LABEL,
    "claim_status_code": ColumnRole.LABEL,
    "is_denied": ColumnRole.LABEL,
    "denial_category": ColumnRole.LABEL,
    "preventability": ColumnRole.LABEL,
    "days_dos_to_payment": ColumnRole.LABEL,
    "days_submission_to_adjudication": ColumnRole.LABEL,
    "final_outcome": ColumnRole.LABEL,
    "rework_touches": ColumnRole.LABEL,
    "net_collected": ColumnRole.LABEL,
    "underpayment_flag": ColumnRole.LABEL,
    # ---- provenance --------------------------------------------------------
    #: DR-09's maturity date. Derived from the date of service and a
    #: configured window, so it is knowable at submission -- but it is not a
    #: fact about the claim a model should read, it is a rule about when the
    #: row may be used.
    "outcome_matured_at": ColumnRole.PROVENANCE,
    #: True when the as-of date fell back to the archival timestamp, which
    #: means the trailing features on that row were computed against the day
    #: the file was ingested rather than the day the claim was sent.
    "submission_date_is_archival": ColumnRole.PROVENANCE,
}

#: Columns that are present and typed but have no source yet, with the
#: milestone that fills each one.
#:
#: They are in the mart rather than waiting outside it because SRS 5.5 is a
#: contract: something downstream is written against this shape, and a column
#: that appears later is a schema change nobody planned for. They are listed
#: here so "it is null" is a recorded state with an owner rather than a
#: surprise, and so the leakage suite can report how much of the dataset is
#: still placeholder.
PENDING_COLUMNS: Final[dict[str, str]] = {
    "rendering_npi": "M1 gap: 837 loop 2310B is not parsed",
    "taxonomy": "M1 gap: 837 loop 2000A PRV is not parsed",
    "ndc_code": "M1 gap: 837 LIN segment is not parsed",
    "specialty": "M3: provider registry",
    "patient_age_bucket": "M3: patient module",
    "patient_sex": "M3: patient module",
    "patient_state": "M3: patient module",
    "eligibility_checked": "M3: eligibility module",
    "eligibility_result": "M3: eligibility module",
    "auth_required": "M3: authorisation module",
    "coder_id_hashed": "M3: the 837 names no coder",
    "scrub_flags_raised": "M4: rules engine",
    "scrub_flags_overridden": "M4: rules engine",
    "denial_category": "M4.8: denial taxonomy",
    "preventability": "M4.8: denial taxonomy",
    "underpayment_flag": "M4.10: contracted rates",
}


def columns_with_role(role: ColumnRole) -> frozenset[str]:
    return frozenset(name for name, value in MART_COLUMNS.items() if value is role)


#: What a model is allowed to read. Everything else is an answer, a key, or a
#: note about how the row was built.
FEATURE_COLUMNS: Final[frozenset[str]] = columns_with_role(ColumnRole.FEATURE)
LABEL_COLUMNS: Final[frozenset[str]] = columns_with_role(ColumnRole.LABEL)
KEY_COLUMNS: Final[frozenset[str]] = columns_with_role(ColumnRole.KEY)
