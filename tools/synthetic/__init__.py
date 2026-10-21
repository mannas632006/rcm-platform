"""Synthetic claim and remittance generator (M0.10, CLAUDE.md section 8).

Produces patients, providers and encounters; valid 837P claims with realistic
CPT, ICD and modifier combinations; a fake payer adjudicator applying NCCI
edits, timely filing, authorisation and eligibility rules; and 835 remittances
with a realistic CARC mix and correct CAS group codes. Deliberate edge cases are
included: multi-CAS lines, reversals, recoupments, bulk 835s spanning many
claims, resubmissions, secondary coverage, zero-payment remittances and
unbalanced files for quarantine testing.

**This corpus must not be used to train any production model.**

CLAUDE.md section 8 requires that assertion to live here, and the reason is worth
stating rather than citing. A model trained on these files learns the rules in
`generate.py` -- the denial probabilities, the payer quirks, the NCCI pairs this
module happens to encode. It will score well against a held-out split of the same
generator, because the split shares those rules, and that score predicts nothing
about real payer behaviour. Measuring it and believing it is the failure mode;
the number looks like evidence and is not.

What this corpus is for: exercising the ingestion pipeline, the balance
assertions, the quarantine path, the state machine, the rules engine's
evaluation order, the work queues and the screens. Every one of those is logic
whose correctness is independent of whether the underlying denial pattern is
real.

`rcm.decision.ml` must refuse a training run whose data provenance is this
module. The check belongs there, at M6, not here -- a generator cannot prevent
its own output being misused downstream.
"""

from __future__ import annotations

#: Marks every manifest this package writes. The training pipeline (M6) refuses
#: a corpus carrying it; see the module docstring.
PROVENANCE: str = "synthetic:tools.synthetic"

__all__ = ["PROVENANCE"]
